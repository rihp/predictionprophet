import typer
import concurrent.futures
import numpy as np
import os
import pandas as pd
import time
import typing as t
from tqdm import tqdm
from collections import defaultdict
from langchain_community.callbacks import get_openai_callback

from evo_researcher.functions.utils import check_not_none
from evo_researcher.autonolas.research import EmbeddingModel
from evo_researcher.benchmark.agents import (
    AbstractBenchmarkedAgent,
    EvoAgent,
    OlasAgent,
    RandomAgent,
    QuestionOnlyAgent,
    FixedAgent,
)
from evo_researcher.benchmark.utils import (
    Market,
    MarketSource,
    Prediction,
    PredictionsCache,
    get_llm_api_call_cost,
    get_markets,
    should_not_happen,
)
from evo_researcher.functions.cache import ENABLE_CACHE 


class Benchmarker:
    def __init__(
        self,
        markets: t.List[Market],
        agents: t.List[AbstractBenchmarkedAgent],
        metric_fns: t.Dict[str, t.Callable[[list[Prediction], list[Market]], str | float | None]] = {},
        cache_path: t.Optional[str] = None,
        only_cached: bool = False,
    ):
        self.registered_agents: t.List[AbstractBenchmarkedAgent] = agents
        if len(set(a.agent_name for a in self.registered_agents)) != len(self.registered_agents):
            raise ValueError("Agents must have unique names")

        # Predictions
        self.cache_path = cache_path
        if self.cache_path and os.path.exists(self.cache_path):
            self.predictions = PredictionsCache.load(path=self.cache_path)
        else:
            self.predictions = PredictionsCache(predictions={})

        self.only_cached = only_cached
        self.markets: list[Market] = [
            m for m in markets 
            if all(self.predictions.has_market(agent_name=agent.agent_name, question=m.question) for agent in self.registered_agents)
        ] if self.only_cached else markets

        # Metrics
        self.metric_fns = metric_fns
        predefined_metric_fns = {
            "MSE for `p_yes`": self._compute_mse,
            "Mean confidence": self._compute_mean_confidence,
            "% within +-0.05": lambda predictions, markets: self._compute_percentage_within_range(
                predictions, markets, tolerance=0.05
            ),
            "% within +-0.1": lambda predictions, markets: self._compute_percentage_within_range(
                predictions, markets, tolerance=0.1
            ),
            "% within +-0.2": lambda predictions, markets: self._compute_percentage_within_range(
                predictions, markets, tolerance=0.2
            ),
            "% correct outcome": self._compute_correct_outcome_percentage,
            "confidence/p_yes error correlation": self._compute_confidence_p_yes_error_correlation,
            "Mean info_utility": self._compute_mean_info_utility,
            "Proportion answerable": self._compute_ratio_evaluated_as_answerable,
            "Proportion answered": self._compute_ratio_answered,
            "Mean cost ($)": self._compute_mean_cost,
            "Mean time (s)": self._compute_mean_time,
        }
        self.metric_fns.update(predefined_metric_fns)

    def add_prediction(
        self,
        agent: AbstractBenchmarkedAgent,
        prediction: Prediction,
        market_question: str,
    ) -> None:
        self.predictions.add_prediction(
            agent_name=agent.agent_name,
            question=market_question,
            prediction=prediction,
        )

    def get_prediction(self, agent_name: str, question: str) -> Prediction:
        return self.predictions.get_prediction(agent_name=agent_name, question=question)

    def run_agents(self) -> None:
        for agent in tqdm(self.registered_agents, desc="Running agents"):
            # Filter out cached predictions
            markets_to_run = [
                m
                for m in self.markets
                if not self.predictions.has_market(
                    agent_name=agent.agent_name, question=m.question
                )
            ]

            def get_prediction_result(market: Market) -> tuple[str, Prediction]:
                with get_openai_callback() as cb:
                    start = time.time()
                    prediction = agent.evaluate_research_predict(
                        market_question=market.question
                    )
                   
                    # Set time only if we aren't using cache, otherwise it won't be accurate. 
                    prediction.time = time.time() - start if not ENABLE_CACHE else None

                    if cb.total_tokens > 0 and cb.total_cost == 0:
                        # TODO: this is a hack to get the cost for an unsupported model
                        cb.total_cost = get_llm_api_call_cost(
                            model=agent.model,
                            prompt_tokens=cb.prompt_tokens,
                            completion_tokens=cb.completion_tokens,
                        )
                    prediction.cost = cb.total_cost
                return market.question, prediction

            # Run agents in parallel
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=agent.max_workers
            ) as executor:
                futures = [executor.submit(get_prediction_result, market) for market in markets_to_run]
                for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc=f"Running {agent.agent_name}"):
                    market_question, prediction = future.result()
                    self.add_prediction(
                        agent=agent,
                        prediction=prediction,
                        market_question=market_question,
                    )
                    if self.cache_path:
                        self.predictions.save(self.cache_path)

    @staticmethod
    def filter_predictions_for_answered(predictions: list[Prediction], markets: list[Market]) -> t.Tuple[list[Prediction], list[Market]]:
        filtered_predictions, filtered_markets = [], []
        for p, m in zip(predictions, markets):
            if p.is_answered:
                filtered_predictions.append(p)
                filtered_markets.append(m)
        return filtered_predictions, filtered_markets

    def _compute_mse(self, predictions: t.List[Prediction], markets: t.List[Market]) -> float | None:
        predictions, markets = self.filter_predictions_for_answered(predictions, markets)
        if not predictions:
            return None
        mse = sum([(check_not_none(p.outcome_prediction).p_yes - m.p_yes) ** 2 for p, m in zip(predictions, markets)]) / len(predictions)
        return mse
 
    def _compute_mean_confidence(
        self, predictions: t.List[Prediction], markets: t.List[Market]
    ) -> float | None:
        predictions, markets = self.filter_predictions_for_answered(predictions, markets)
        if not predictions:
            return None
        mean_confidence = sum([check_not_none(p.outcome_prediction).confidence for p in predictions]) / len(predictions)
        return mean_confidence

    def _compute_mean_info_utility(
        self, predictions: t.List[Prediction], markets: t.List[Market]
    ) -> float | None:
        predictions, markets = self.filter_predictions_for_answered(predictions, markets)
        predictions_with_info_utility = [p for p in predictions if check_not_none(p.outcome_prediction).info_utility is not None]
        if not predictions_with_info_utility:
            return None
        mean_info_utility = sum([check_not_none(check_not_none(p.outcome_prediction).info_utility) for p in predictions_with_info_utility]) / len(
            predictions_with_info_utility
        )
        return mean_info_utility

    def _compute_percentage_within_range(
        self,
        predictions: t.List[Prediction],
        markets: t.List[Market],
        tolerance: float = 0.05,
    ) -> float | None:
        predictions, markets = self.filter_predictions_for_answered(predictions, markets)
        if not predictions:
            return None

        within_range_count = 0
        for p, m in zip(predictions, markets):
            if abs(check_not_none(p.outcome_prediction).p_yes - m.p_yes) <= tolerance:
                within_range_count += 1

        return (100 * within_range_count) / len(predictions)

    def _compute_correct_outcome_percentage(
        self, predictions: t.List[Prediction], markets: t.List[Market]
    ) -> float | None:
        predictions, markets = self.filter_predictions_for_answered(predictions, markets)
        if not predictions:
            return None

        correct_outcome_count = 0
        for p, m in zip(predictions, markets):
            if (check_not_none(p.outcome_prediction).p_yes > 0.5 and m.p_yes > 0.5) or (check_not_none(p.outcome_prediction).p_yes < 0.5 and m.p_yes < 0.5):
                correct_outcome_count += 1

        return (100 * correct_outcome_count) / len(predictions)

    def _compute_confidence_p_yes_error_correlation(
        self, predictions: t.List[Prediction], markets: t.List[Market]
    ) -> float | None:
        predictions, markets = self.filter_predictions_for_answered(predictions, markets)
        if not predictions:
            return None

        p_yes_errors = [abs(check_not_none(p.outcome_prediction).p_yes - m.p_yes) for p, m in zip(predictions, markets)]
        confidences = [check_not_none(p.outcome_prediction).confidence for p in predictions]
        return float(np.corrcoef(confidences, p_yes_errors)[0, 1])

    def _compute_mean_cost(
        self, predictions: t.List[Prediction], markets: t.List[Market]
    ) -> float | None:
        # Note: costs are optional
        costs = [p.cost for p in predictions if p.cost]
        if costs:
            return sum(costs) / len(costs)
        else:
            return None

    def _compute_mean_time(
        self, predictions: t.List[Prediction], markets: t.List[Market]
    ) -> float | None:
        # Note: times are optional
        times = [p.time for p in predictions if p.time]
        if times:
            return sum(times) / len(times)
        else:
            return None
        
    def _compute_ratio_evaluated_as_answerable(self, predictions: t.List[Prediction], markets: t.List[Market]) -> float:
        return sum(1 for p in predictions if p.evaluation and p.evaluation.is_predictable) / len(predictions)
       
    def _compute_ratio_answered(self, predictions: t.List[Prediction], markets: t.List[Market]) -> float:
        return sum(1 for p in predictions if p.is_answered) / len(predictions)
       
    def compute_metrics(self) -> t.Dict[str, t.List[t.Any]]:
        metrics: dict[str, list[str | float | None]] = {}
        metrics["Agents"] = [a.agent_name for a in self.registered_agents]

        for name, fn in self.metric_fns.items():
            metrics[name] = []
            for agent in self.registered_agents:
                ordered_predictions = [
                    self.get_prediction(question=market.question, agent_name=agent.agent_name)
                    for market in self.markets
                ]
                metrics[name].append(fn(ordered_predictions, self.markets))

        return metrics

    def get_markets_summary(self) -> t.Dict[str, t.List[str | float]]:
        market_questions = [q.question for q in self.markets]
        urls = [q.url for q in self.markets]
        markets_summary: dict[str, list[str | float]] = {
            "Market Question": [
                f"[{question}]({url})" for question, url in zip(market_questions, urls)
            ],
        }

        for agent in [a.agent_name for a in self.registered_agents]:
            agent_predictions = [self.get_prediction(agent_name=agent, question=q) for q in market_questions]
            markets_summary[f"{agent} p_yes"] = [
                (
                    p.outcome_prediction.p_yes 
                    if p.evaluation and p.evaluation.is_predictable and p.outcome_prediction  # Is answerable and answered
                    else "N/A" 
                    if not p.evaluation and not p.outcome_prediction # Not evaluated for some reason
                    else "S" 
                    if p.evaluation and not p.evaluation.is_predictable  # Skipped (evaluated to be not predictable)
                    else "F" 
                    if p.evaluation and p.evaluation.is_predictable and not p.outcome_prediction # Failed (no prediction)
                    else should_not_happen(f"Unexpected case in get_markets_summary() for {p}.")
                )
                for p in agent_predictions
            ]
        markets_summary[f"reference p_yes"] = [m.p_yes for m in self.markets]
        return markets_summary
    
    def calculate_expected_returns(self, prediction: Prediction, market: Market) -> float | None:
        if not prediction.is_answered:
            return None

        # TODO: Add support for different bet sizes and calculate shares based on the market's odds.
        bet_units = 10  # Assuming the agent always bet 10 units per market.
        receive_shares = 20  # Because we assume markets trades at 50/50.
        buy_yes_threshold = 0.5  # If the agent's prediction is > 50% it should buy "yes", otherwise "no".

        assert prediction.outcome_prediction is not None
        yes_shares = receive_shares if prediction.outcome_prediction.p_yes > buy_yes_threshold else 0
        no_shares = receive_shares if prediction.outcome_prediction.p_yes <= buy_yes_threshold else 0
        
        expected_returns_pct = (
            yes_shares * market.p_yes  
            + no_shares * (1 - market.p_yes)
            - bet_units
        )
        expected_returns = 100 * expected_returns_pct / bet_units

        return expected_returns

    def compute_expected_returns_summary(self) -> t.Tuple[dict[str, list[str | float]], dict[str, list[str | float | None]]]:
        overall_summary: dict[str, list[str | float]] = defaultdict(list)

        for agent in self.registered_agents:
            expected_returns = []

            for market in self.markets:
                if (prediction := self.get_prediction(agent.agent_name, market.question)).is_answered:
                    expected_returns.append(check_not_none(self.calculate_expected_returns(prediction, market)))

            overall_summary["Agent"].append(agent.agent_name)
            overall_summary["Mean expected returns"].append(float(np.mean(expected_returns)))
            overall_summary["Median expected returns"].append(float(np.median(expected_returns)))
            overall_summary["Total expected returns"].append(float(np.sum(expected_returns)))

        per_market: dict[str, list[str | float | None]]  = defaultdict(list)

        for market in self.markets:
            per_market["Market Question"].append(market.question)

            for agent in self.registered_agents:
                per_market[agent.agent_name].append(self.calculate_expected_returns(self.get_prediction(agent.agent_name, market.question), market))

        return dict(overall_summary), dict(per_market)

    def generate_markdown_report(self) -> str:
        md = "# Comparison Report\n\n"
        md += "## Summary Statistics\n\n"
        md += pd.DataFrame(self.compute_metrics()).to_markdown(index=False)
        md += "\n\n"
        md += "## Markets\n\n"
        md += pd.DataFrame(self.get_markets_summary()).to_markdown(index=False)
        md += "\n\n"
        md += "## Expected value\n\n"
        overall_summary, per_market = self.compute_expected_returns_summary()
        md += pd.DataFrame(overall_summary).to_markdown(index=False)
        md += "\n\n"
        md += pd.DataFrame(per_market).to_markdown(index=False)
        return md


def main(
    n: int = 10,
    output: str = "./benchmark_report.md",
    reference: MarketSource = MarketSource.MANIFOLD,
    max_workers: int = 1,
    cache_path: t.Optional[str] = "predictions_cache.json",
    only_cached: bool = False,
) -> None:
    markets = get_markets(number=n, source=reference)
    markets_deduplicated = list(({m.question: m for m in markets}.values()))  
    if len(markets) != len(markets_deduplicated):
        print(f"Warning: Deduplicated markets from {len(markets)} to {len(markets_deduplicated)}.")

    benchmarker = Benchmarker(
        markets=markets_deduplicated,
        agents=[
            RandomAgent(agent_name="random", max_workers=max_workers),
            QuestionOnlyAgent(model="gpt-3.5-turbo-0125", agent_name="question-only_gpt-3.5-turbo-0125", max_workers=max_workers),
            FixedAgent(fixed_answer=False, agent_name="fixed-no", max_workers=max_workers),
            OlasAgent(model="gpt-3.5-turbo", max_workers=max_workers, agent_name="olas_gpt-3.5-turbo_t0.7", temperature=0.7),  # Reference configuration.
            OlasAgent(model="gpt-3.5-turbo", max_workers=max_workers, agent_name="olas_gpt-3.5-turbo"),  
            OlasAgent(model="gpt-3.5-turbo-0125", max_workers=max_workers, agent_name="olas_gpt-3.5-turbo-0125"),  
            OlasAgent(model="gpt-3.5-turbo-0125", max_workers=max_workers, agent_name="olas_gpt-3.5-turbo-0125_openai-embeddings", embedding_model=EmbeddingModel.openai),  
            EvoAgent(model="gpt-3.5-turbo-0125", max_workers=max_workers, agent_name="evo_gpt-3.5-turbo-0125_summary", use_summaries=True),
            EvoAgent(model="gpt-3.5-turbo-0125", max_workers=max_workers, agent_name="evo_gpt-3.5-turbo-0125"),
            # EvoAgent(model="gpt-4-1106-preview", max_workers=max_workers, agent_name="evo_gpt-4-1106-preview"),  # Too expensive to be enabled by default.
        ],
        cache_path=cache_path,
        only_cached=only_cached,
    )

    benchmarker.run_agents()
    md = benchmarker.generate_markdown_report()

    with open(output, "w") as f:
        print(f"Writing benchmark report to: {output}")
        f.write(md)


if __name__ == "__main__":
    typer.run(main)