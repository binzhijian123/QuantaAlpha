import hashlib
import json
import re
from pathlib import Path
from typing import Any, List, Tuple

from jinja2 import Environment, StrictUndefined

from quantaalpha.factors.coder.factor import FactorExperiment, FactorTask
from quantaalpha.components.proposal import FactorHypothesis2Experiment, FactorHypothesisGen
from quantaalpha.core.prompts import Prompts
from quantaalpha.core.proposal import Hypothesis, Scenario, Trace
from quantaalpha.core.experiment import Experiment
from quantaalpha.factors.experiment import QlibFactorExperiment
from quantaalpha.llm.client import APIBackend, robust_json_parse
from quantaalpha.llm.config import LLM_SETTINGS
import os
import pandas as pd
from quantaalpha.log import logger
from quantaalpha.factors.regulator.factor_regulator import FactorRegulator
from quantaalpha.core.exception import FactorEmptyError

DEFAULT_HISTORY_LIMIT = 6
MIN_HISTORY_LIMIT = 1


def render_hypothesis_and_feedback(prompt_dict, trace: Trace, history_limit: int = DEFAULT_HISTORY_LIMIT) -> str:
    """Render hypothesis_and_feedback with configurable history limit."""
    if len(trace.hist) > 0:
        limited_trace = Trace(scen=trace.scen)
        limited_trace.hist = trace.hist[-history_limit:] if history_limit > 0 else trace.hist
        return (
            Environment(undefined=StrictUndefined)
            .from_string(prompt_dict["hypothesis_and_feedback"])
            .render(trace=limited_trace)
        )
    else:
        return "No previous hypothesis and feedback available since it's the first round."


def is_input_length_error(error_msg: str) -> bool:
    """Check if error is due to input length limit."""
    error_indicators = [
        "input length",
        "context length", 
        "maximum context",
        "token limit",
        "InvalidParameter",
        "Range of input length",
        "max_tokens",
        "too long"
    ]
    error_str = str(error_msg).lower()
    return any(indicator.lower() in error_str for indicator in error_indicators)


QlibFactorHypothesis = Hypothesis
qa_prompt_dict = Prompts(file_path=Path(__file__).parent / "prompts" / "proposal.yaml")

class AlphaAgentHypothesis(Hypothesis):
    """
    AlphaAgentHypothesis extends the Hypothesis class to include a potential_direction,
    which represents the initial idea or starting point for the hypothesis.
    """

    def __init__(
        self,
        hypothesis: str,
        concise_observation: str,
        concise_justification: str,
        concise_knowledge: str,
        concise_specification: str
    ) -> None:
        super().__init__(
            hypothesis,
            "",
            "",
            concise_observation,
            concise_justification,
            concise_knowledge,
        )
        self.concise_specification = concise_specification
        
    def __str__(self) -> str:
        return f"""Hypothesis: {self.hypothesis}
                Concise Observation: {self.concise_observation}
                Concise Justification: {self.concise_justification}
                Concise Knowledge: {self.concise_knowledge}
                concise Specification: {self.concise_specification}
                """

base_prompt_dict = Prompts(file_path=Path(__file__).parent / "prompts" / "prompts.yaml")

class QlibFactorHypothesisGen(FactorHypothesisGen):
    def __init__(self, scen: Scenario) -> Tuple[dict, bool]:
        super().__init__(scen)

    def prepare_context(self, trace: Trace) -> Tuple[dict, bool]:
        hypothesis_and_feedback = (
            (
                Environment(undefined=StrictUndefined)
                .from_string(base_prompt_dict["hypothesis_and_feedback"])
                .render(trace=trace)
            )
            if len(trace.hist) > 0
            else "No previous hypothesis and feedback available since it's the first round."
        )
        context_dict = {
            "hypothesis_and_feedback": hypothesis_and_feedback,
            "RAG": None,
            "hypothesis_output_format": base_prompt_dict["hypothesis_output_format"],
            "hypothesis_specification": base_prompt_dict["factor_hypothesis_specification"],
        }
        return context_dict, True

    def convert_response(self, response: str) -> Hypothesis:
        response_dict = robust_json_parse(response)
        hypothesis = QlibFactorHypothesis(
            hypothesis=response_dict.get("hypothesis", ""),
            reason=response_dict.get("reason", ""),
            concise_reason=response_dict.get("concise_reason", ""),
            concise_observation=response_dict.get("concise_observation", ""),
            concise_justification=response_dict.get("concise_justification", ""),
            concise_knowledge=response_dict.get("concise_knowledge", ""),
        )
        return hypothesis


class QlibFactorHypothesis2Experiment(FactorHypothesis2Experiment):
    def prepare_context(self, hypothesis: Hypothesis, trace: Trace) -> Tuple[dict | bool]:
        scenario = trace.scen.get_scenario_all_desc()
        experiment_output_format = base_prompt_dict["factor_experiment_output_format"]

        hypothesis_and_feedback = (
            (
                Environment(undefined=StrictUndefined)
                .from_string(base_prompt_dict["hypothesis_and_feedback"])
                .render(trace=trace)
            )
            if len(trace.hist) > 0
            else "No previous hypothesis and feedback available since it's the first round."
        )

        experiment_list: List[FactorExperiment] = [t[1] for t in trace.hist]

        factor_list = []
        for experiment in experiment_list:
            factor_list.extend(experiment.sub_tasks)

        target_hypothesis = str(hypothesis)
        evolution_instruction = str(
            getattr(self, "evolution_instruction", "") or ""
        ).strip()
        if evolution_instruction:
            target_hypothesis += (
                "\n\nEvolution repair instruction for this factor-construction "
                f"action:\n{evolution_instruction}"
            )

        return {
            "target_hypothesis": target_hypothesis,
            "scenario": scenario,
            "hypothesis_and_feedback": hypothesis_and_feedback,
            "experiment_output_format": experiment_output_format,
            "target_list": factor_list,
            "RAG": None,
        }, True

    def convert_response(self, response: str, trace: Trace) -> FactorExperiment:
        response_dict = robust_json_parse(response)
        tasks = []

        for factor_name in response_dict:
            factor_data = response_dict.get(factor_name, {})
            if not isinstance(factor_data, dict):
                continue
            description = factor_data.get("description", "")
            formulation = factor_data.get("formulation", "")
            # expression = factor_data.get("expression", "")
            variables = factor_data.get("variables", {})
            tasks.append(
                FactorTask(
                    factor_name=factor_name,
                    factor_description=description,
                    factor_formulation=formulation,
                    # factor_expression=expression,
                    variables=variables,
                )
            )

        exp = QlibFactorExperiment(tasks)
        exp.based_experiments = [QlibFactorExperiment(sub_tasks=[])] + [t[1] for t in trace.hist if t[2]]

        unique_tasks = []

        for task in tasks:
            duplicate = False
            for based_exp in exp.based_experiments:
                for sub_task in based_exp.sub_tasks:
                    if task.factor_name == sub_task.factor_name:
                        duplicate = True
                        break
                if duplicate:
                    break
            if not duplicate:
                unique_tasks.append(task)

        exp.tasks = unique_tasks
        return exp



qa_prompt_dict = Prompts(file_path=Path(__file__).parent / "prompts" / "prompts.yaml")

# prompt_dict not as attribute: class instance is pickled later, prompt_dict cannot be pickled
class AlphaAgentHypothesisGen(FactorHypothesisGen):
    def __init__(self, scen: Scenario, potential_direction: str=None) -> Tuple[dict, bool]:
        super().__init__(scen)
        self.potential_direction = potential_direction

    def prepare_context(self, trace: Trace, history_limit: int = DEFAULT_HISTORY_LIMIT) -> Tuple[dict, bool]:
        
        if len(trace.hist) > 0:
            hypothesis_and_feedback = render_hypothesis_and_feedback(
                qa_prompt_dict, trace, history_limit
            )
            
        elif self.potential_direction is not None: 
            hypothesis_and_feedback = (
                Environment(undefined=StrictUndefined)
                .from_string(qa_prompt_dict["potential_direction_transformation"])
                .render(potential_direction=self.potential_direction)
            ) # 
        else:
            hypothesis_and_feedback = "No previous hypothesis and feedback available since it's the first round. You are encouraged to propose an innovative hypothesis that diverges significantly from existing perspectives."
            
        context_dict = {
            "hypothesis_and_feedback": hypothesis_and_feedback,
            "RAG": None,
            "hypothesis_output_format": qa_prompt_dict["hypothesis_output_format"],
            "hypothesis_specification": qa_prompt_dict["factor_hypothesis_specification"],
        }
        return context_dict, True

    def convert_response(self, response: str) -> AlphaAgentHypothesis:
        """
        Convert LLM JSON to AlphaAgentHypothesis; use default empty string for missing fields to avoid KeyError.
        """
        response_dict = robust_json_parse(response)
        # Use get to avoid KeyError on missing fields
        hypothesis = AlphaAgentHypothesis(
            hypothesis=response_dict.get("hypothesis", ""),
            concise_observation=response_dict.get("concise_observation", ""),
            concise_knowledge=response_dict.get("concise_knowledge", ""),
            concise_justification=response_dict.get("concise_justification", ""),
            concise_specification=response_dict.get("concise_specification", ""),
        )
        return hypothesis
    
    def gen(self, trace: Trace) -> AlphaAgentHypothesis:
        """Generate hypothesis; supports dynamic history limit for input length."""
        history_limit = DEFAULT_HISTORY_LIMIT
        
        while history_limit >= MIN_HISTORY_LIMIT:
            try:
                context_dict, json_flag = self.prepare_context(trace, history_limit)
                system_prompt = (
                    Environment(undefined=StrictUndefined)
                    .from_string(qa_prompt_dict["hypothesis_gen"]["system_prompt"])
                    .render(
                        targets=self.targets,
                        scenario=self.scen.get_scenario_all_desc(filtered_tag="hypothesis_and_experiment"),
                        hypothesis_output_format=context_dict["hypothesis_output_format"],
                        hypothesis_specification=context_dict["hypothesis_specification"],
                    )
                )
                user_prompt = (
                    Environment(undefined=StrictUndefined)
                    .from_string(qa_prompt_dict["hypothesis_gen"]["user_prompt"])
                    .render(
                        targets=self.targets,
                        hypothesis_and_feedback=context_dict["hypothesis_and_feedback"],
                        RAG=context_dict["RAG"],
                        round=len(trace.hist)
                    )
                )

                resp = APIBackend().build_messages_and_create_chat_completion(user_prompt, system_prompt, json_mode=json_flag)
                hypothesis = self.convert_response(resp)
                return hypothesis
            
            except Exception as e:
                if is_input_length_error(str(e)) and history_limit > MIN_HISTORY_LIMIT:
                    history_limit -= 1
                    logger.warning(f"Input length exceeded, retrying with history_limit={history_limit}...")
                else:
                    raise
        
        # Last attempt with minimum history limit
        context_dict, json_flag = self.prepare_context(trace, MIN_HISTORY_LIMIT)
        system_prompt = (
            Environment(undefined=StrictUndefined)
            .from_string(qa_prompt_dict["hypothesis_gen"]["system_prompt"])
            .render(
                targets=self.targets,
                scenario=self.scen.get_scenario_all_desc(filtered_tag="hypothesis_and_experiment"),
                hypothesis_output_format=context_dict["hypothesis_output_format"],
                hypothesis_specification=context_dict["hypothesis_specification"],
            )
        )
        user_prompt = (
            Environment(undefined=StrictUndefined)
            .from_string(qa_prompt_dict["hypothesis_gen"]["user_prompt"])
            .render(
                targets=self.targets,
                hypothesis_and_feedback=context_dict["hypothesis_and_feedback"],
                RAG=context_dict["RAG"],
                round=len(trace.hist)
            )
        )
        resp = APIBackend().build_messages_and_create_chat_completion(user_prompt, system_prompt, json_mode=json_flag)
        hypothesis = self.convert_response(resp)
        return hypothesis
    
    

class EmptyHypothesisGen(FactorHypothesisGen):
    def __init__(self, scen: Scenario) -> Tuple[dict, bool]:
        super().__init__(scen)
        
    def convert_response(self, *args, **kwargs) -> AlphaAgentHypothesis: 
        return super().convert_response(*args, **kwargs)  
    
    def prepare_context(self, *args, **kwargs) -> Tuple[dict | bool]:
        return super().prepare_context(*args, **kwargs)

    def gen(self, trace: Trace) -> AlphaAgentHypothesis:

        hypothesis = AlphaAgentHypothesis(
            hypothesis="",
            concise_observation="",
            concise_justification="",
            concise_knowledge="",
            concise_specification=""
        )

        return hypothesis




class AlphaAgentHypothesis2FactorExpression(FactorHypothesis2Experiment):
    def __init__(
        self,
        *args,
        consistency_enabled: bool = False,
        complexity_enabled: bool = True,
        redundancy_enabled: bool = True,
        factors_per_hypothesis: int = 1,
        generation_max_attempts: int = 5,
        require_exact_factor_count: bool = False,
        variant_max_attempts: int = 2,
        variant_contract: dict[str, Any] | None = None,
        consistency_strict_mode: bool = False,
        consistency_require_pass: bool = False,
        quality_gate_fail_closed_on_error: bool = False,
        max_correction_attempts: int = 3,
        symbol_length_threshold: int | None = None,
        base_features_threshold: int | None = None,
        free_args_ratio_threshold: float | None = None,
        unique_vars_ratio_threshold: float | None = None,
        duplication_threshold: int | None = None,
        factor_zoo_path: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.factors_per_hypothesis = max(1, int(factors_per_hypothesis or 1))
        self.generation_max_attempts = max(1, int(generation_max_attempts or 1))
        self.require_exact_factor_count = bool(require_exact_factor_count)
        self.variant_max_attempts = max(1, int(variant_max_attempts or 1))
        self.variant_contract = (
            dict(variant_contract)
            if isinstance(variant_contract, dict)
            else {}
        )
        # Initialize FactorRegulator with config settings
        from quantaalpha.factors.coder.config import FACTOR_COSTEER_SETTINGS
        self.factor_regulator = FactorRegulator(
            factor_zoo_path=(
                factor_zoo_path
                if factor_zoo_path is not None
                else FACTOR_COSTEER_SETTINGS.factor_zoo_path
            ),
            duplication_threshold=(
                duplication_threshold
                if duplication_threshold is not None
                else FACTOR_COSTEER_SETTINGS.duplication_threshold
            ),
            symbol_length_threshold=(
                symbol_length_threshold
                if symbol_length_threshold is not None
                else FACTOR_COSTEER_SETTINGS.symbol_length_threshold
            ),
            base_features_threshold=(
                base_features_threshold
                if base_features_threshold is not None
                else FACTOR_COSTEER_SETTINGS.base_features_threshold
            ),
            free_args_ratio_threshold=(
                free_args_ratio_threshold
                if free_args_ratio_threshold is not None
                else FACTOR_COSTEER_SETTINGS.free_args_ratio_threshold
            ),
            unique_vars_ratio_threshold=(
                unique_vars_ratio_threshold
                if unique_vars_ratio_threshold is not None
                else FACTOR_COSTEER_SETTINGS.unique_vars_ratio_threshold
            ),
        )
        
        # Initialize consistency checker if enabled
        self.consistency_enabled = consistency_enabled
        self.complexity_enabled = complexity_enabled
        self.redundancy_enabled = redundancy_enabled
        self.consistency_strict_mode = consistency_strict_mode
        self.consistency_require_pass = consistency_require_pass
        self.quality_gate_fail_closed_on_error = (
            quality_gate_fail_closed_on_error
        )
        self.max_correction_attempts = max(1, int(max_correction_attempts or 1))
        self._quality_gate = None

    def _append_factor_count_instruction(
        self,
        prompt: str,
        factor_count: int | None = None,
    ) -> str:
        count = (
            self.factors_per_hypothesis
            if factor_count is None
            else max(1, int(factor_count))
        )
        factor_word = "factor" if count == 1 else "factors"
        return (
            f"{prompt}\n\n"
            "Runtime generation-count override: "
            f"Generate exactly {count} {factor_word}. "
            "This overrides any generic 2-3 factor instruction above."
        )

    def _limit_response_dict(
        self,
        response_dict: dict,
        factor_count: int | None = None,
    ) -> dict:
        count = (
            self.factors_per_hypothesis
            if factor_count is None
            else max(1, int(factor_count))
        )
        return dict(list(response_dict.items())[:count])

    def _generate_candidate_response(
        self,
        hypothesis: Hypothesis,
        user_prompt: str,
        system_prompt: str,
        json_flag: bool,
        context: dict,
        *,
        expected_factor_count: int | None = None,
        max_attempts_override: int | None = None,
    ) -> tuple[str, list[str], list[str]]:
        """Generate and validate a factor response within a finite attempt budget."""
        expression_duplication_prompt = None
        last_rejection_reason = "no valid factor returned"
        max_attempts = max(
            1,
            int(
                max_attempts_override
                if max_attempts_override is not None
                else getattr(self, "generation_max_attempts", 5) or 5
            ),
        )
        target_count = (
            self.factors_per_hypothesis
            if expected_factor_count is None
            else max(1, int(expected_factor_count))
        )

        for attempt in range(1, max_attempts + 1):
            resp = APIBackend().build_messages_and_create_chat_completion(
                user_prompt,
                system_prompt,
                json_mode=json_flag,
            )
            try:
                parsed_response = robust_json_parse(resp)
            except json.JSONDecodeError as exc:
                last_rejection_reason = f"JSON parse failed: {exc}"
                logger.warning(
                    f"{last_rejection_reason} "
                    f"(factor generation attempt {attempt}/{max_attempts})"
                )
                continue

            if not isinstance(parsed_response, dict):
                last_rejection_reason = (
                    f"factor response must be a JSON object, got "
                    f"{type(parsed_response).__name__}"
                )
                logger.warning(
                    f"{last_rejection_reason} "
                    f"(factor generation attempt {attempt}/{max_attempts})"
                )
                continue

            if (
                getattr(self, "require_exact_factor_count", False)
                and len(parsed_response) != target_count
            ):
                last_rejection_reason = (
                    "factor response count mismatch: "
                    f"expected exactly {target_count}, got {len(parsed_response)}"
                )
                logger.warning(
                    f"{last_rejection_reason} "
                    f"(factor generation attempt {attempt}/{max_attempts})"
                )
                continue

            response_dict = self._limit_response_dict(
                parsed_response,
                target_count,
            )

            if not response_dict:
                last_rejection_reason = "LLM returned an empty factor object"
                logger.warning(
                    f"{last_rejection_reason} "
                    f"(factor generation attempt {attempt}/{max_attempts})"
                )
                continue

            proposed_names = []
            proposed_exprs = []
            response_accepted = True

            for factor_name, factor_data in response_dict.items():
                if not isinstance(factor_data, dict):
                    last_rejection_reason = f"factor '{factor_name}' is not an object"
                    response_accepted = False
                    break

                expr = factor_data.get("expression", "")
                description = factor_data.get("description", "")
                formulation = factor_data.get("formulation", "")
                variables = factor_data.get("variables", {})
                quality_gate_audit: dict[str, Any] = {}

                if not self.factor_regulator.is_parsable(expr):
                    last_rejection_reason = f"failed to parse expression: {expr}"
                    response_accepted = False
                    break

                success, eval_dict = self.factor_regulator.evaluate(expr)
                if not success:
                    last_rejection_reason = f"failed to evaluate expression: {expr}"
                    response_accepted = False
                    break

                if self.consistency_enabled and self.quality_gate is None:
                    last_rejection_reason = (
                        f"consistency checker unavailable for factor '{factor_name}'"
                    )
                    logger.warning(last_rejection_reason)
                    response_accepted = False
                    break

                if self.consistency_enabled:
                    try:
                        passed, feedback, results = self.quality_gate.evaluate(
                            hypothesis=str(hypothesis),
                            factor_name=factor_name,
                            factor_description=description,
                            factor_formulation=formulation,
                            factor_expression=expr,
                            variables=variables,
                        )

                        corrected_expression = results.get("corrected_expression")
                        if corrected_expression and corrected_expression != expr:
                            logger.info(
                                f"Consistency check corrected expression: {expr} -> {corrected_expression}"
                            )
                            expr = corrected_expression
                            factor_data["expression"] = expr

                            if not self.factor_regulator.is_parsable(expr):
                                last_rejection_reason = f"corrected expression could not be parsed: {expr}"
                                response_accepted = False
                                break
                            success, eval_dict = self.factor_regulator.evaluate(expr)
                            if not success:
                                last_rejection_reason = f"corrected expression could not be evaluated: {expr}"
                                response_accepted = False
                                break

                        corrected_description = results.get("corrected_description")
                        if corrected_description and corrected_description != description:
                            description = corrected_description
                            factor_data["description"] = description

                        if not passed:
                            logger.warning(f"Consistency check failed: {factor_name}, feedback: {feedback}")
                            last_rejection_reason = (
                                f"consistency check rejected factor '{factor_name}': {feedback}"
                            )
                            response_accepted = False
                            break
                        consistency_result = results.get("consistency") or {}
                        quality_gate_audit = {
                            "evaluated": True,
                            "passed": True,
                            "consistency_passed": bool(
                                consistency_result.get("is_consistent", False)
                            ),
                            "consistency_check": consistency_result,
                            "complexity_check": results.get("complexity"),
                            "redundancy_check": results.get("redundancy"),
                        }
                    except Exception as exc:
                        last_rejection_reason = (
                            f"consistency check failed closed for factor '{factor_name}': {exc}"
                        )
                        logger.warning(last_rejection_reason)
                        response_accepted = False
                        break

                if not self.factor_regulator.is_expression_acceptable(
                    eval_dict,
                    complexity_enabled=self.complexity_enabled,
                    redundancy_enabled=self.redundancy_enabled,
                ):
                    num_all_nodes = eval_dict['num_all_nodes']
                    free_args_ratio = (
                        float(eval_dict['num_free_args']) / float(num_all_nodes)
                        if num_all_nodes > 0 else 0.0
                    )
                    unique_vars_ratio = (
                        float(eval_dict['num_unique_vars']) / float(num_all_nodes)
                        if num_all_nodes > 0 else 0.0
                    )
                    feedback_item = (
                        Environment(undefined=StrictUndefined)
                        .from_string(qa_prompt_dict["expression_duplication"])
                        .render(
                            prev_expression=expr,
                            duplicated_subtree_size=eval_dict['duplicated_subtree_size'],
                            duplication_threshold=self.factor_regulator.duplication_threshold,
                            duplicated_subtree=eval_dict.get('duplicated_subtree', ''),
                            matched_alpha=eval_dict.get('matched_alpha', ''),
                            free_args_ratio=free_args_ratio,
                            num_free_args=eval_dict['num_free_args'],
                            unique_vars_ratio=unique_vars_ratio,
                            num_unique_vars=eval_dict['num_unique_vars'],
                            num_all_nodes=num_all_nodes,
                            symbol_length=eval_dict.get('symbol_length', 0),
                            symbol_length_threshold=self.factor_regulator.symbol_length_threshold,
                            num_base_features=eval_dict.get('num_base_features', 0),
                            base_features_threshold=self.factor_regulator.base_features_threshold,
                        )
                    )
                    expression_duplication_prompt = '\n\n'.join(
                        filter(None, [expression_duplication_prompt, feedback_item])
                    )
                    user_prompt = (
                        Environment(undefined=StrictUndefined)
                        .from_string(qa_prompt_dict["hypothesis2experiment"]["user_prompt"])
                        .render(
                            targets=self.targets,
                            target_hypothesis=context["target_hypothesis"],
                            hypothesis_and_feedback=context["hypothesis_and_feedback"],
                            function_lib_description=context["function_lib_description"],
                            target_list=context["target_list"],
                            RAG=context["RAG"],
                            expression_duplication=expression_duplication_prompt,
                        )
                    )
                    user_prompt = self._append_factor_count_instruction(
                        user_prompt,
                        target_count,
                    )
                    last_rejection_reason = f"expression rejected by quality regulator: {expr}"
                    response_accepted = False
                    break

                if quality_gate_audit:
                    factor_data["quality_gate"] = quality_gate_audit
                proposed_names.append(factor_name)
                proposed_exprs.append(expr)

            if response_accepted and len(proposed_names) == len(response_dict):
                normalized_response = json.dumps(response_dict, ensure_ascii=False)
                return normalized_response, proposed_names, proposed_exprs

            logger.warning(
                f"{last_rejection_reason} "
                f"(factor generation attempt {attempt}/{max_attempts})"
            )

        raise FactorEmptyError(
            f"No valid factor response after {max_attempts} attempts: "
            f"{last_rejection_reason}"
        )

    @staticmethod
    def _text_sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _variant_roles(self) -> list[dict[str, Any]]:
        roles = self.variant_contract.get("roles") or []
        return [dict(role) for role in roles if isinstance(role, dict)]

    def _variant_contract_enabled(self) -> bool:
        return bool(self.variant_contract.get("enabled", False))

    def _build_variant_hypothesis(
        self,
        family_hypothesis: str,
        role: dict[str, Any],
    ) -> str:
        role_id = str(role.get("id", "")).strip()
        variation_axis = str(role.get("variation_axis", "")).strip()
        instruction = str(role.get("instruction", "")).strip()
        return (
            "Immutable factor-family hypothesis:\n"
            f"{family_hypothesis}\n\n"
            "Factor-specific variant contract:\n"
            f"- variant_id: {role_id}\n"
            f"- allowed_variation_axis: {variation_axis}\n"
            f"- required_behavior: {instruction}\n"
            "All economic mechanism, prediction direction, target market, "
            "allowed data fields, and non-declared parameters must remain "
            "consistent with the immutable family hypothesis."
        )

    def _build_variant_prompt(
        self,
        base_prompt: str,
        family_id: str,
        role: dict[str, Any],
        accepted_variants: list[dict[str, str]],
    ) -> str:
        role_id = str(role.get("id", "")).strip()
        variation_axis = str(role.get("variation_axis", "")).strip()
        instruction = str(role.get("instruction", "")).strip()
        allowed_features = [
            str(value)
            for value in (self.variant_contract.get("allowed_features") or [])
        ]
        accepted_json = json.dumps(
            accepted_variants,
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            f"{base_prompt}\n\n"
            "Paper-family runtime protocol (highest priority):\n"
            f"- family_id: {family_id}\n"
            f"- generate only variant_id: {role_id}\n"
            f"- the only permitted variation axis is: {variation_axis}\n"
            f"- role instruction: {instruction}\n"
            f"- allowed raw features: {json.dumps(allowed_features)}\n"
            "- Preserve the immutable economic mechanism, prediction sign, "
            "market, and target horizon except where the declared axis "
            "explicitly permits a change.\n"
            "- Return exactly one top-level factor object using the existing "
            "factor schema. No metadata or prose outside that object.\n"
            "- The factor name and expression must differ from every accepted "
            "sibling.\n"
            f"- accepted siblings (frozen, do not rewrite): {accepted_json}"
        )

    def _expression_uses_only_allowed_features(self, expression: str) -> bool:
        allowed = {
            str(value)
            for value in (self.variant_contract.get("allowed_features") or [])
        }
        if not allowed:
            return True
        used = set(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", expression))
        return used.issubset(allowed)

    def _generate_variant_family_response(
        self,
        hypothesis: Hypothesis,
        user_prompt: str,
        system_prompt: str,
        json_flag: bool,
        context: dict,
    ) -> tuple[str, list[str], list[str]]:
        """Generate exactly one independently repairable factor per declared role."""
        roles = self._variant_roles()
        role_ids = [str(role.get("id", "")).strip() for role in roles]
        if (
            len(roles) != self.factors_per_hypothesis
            or not all(role_ids)
            or len(set(role_ids)) != len(role_ids)
        ):
            raise FactorEmptyError(
                "Variant contract must declare exactly "
                f"{self.factors_per_hypothesis} unique non-empty roles"
            )

        family_hypothesis = str(hypothesis)
        family_hypothesis_sha256 = self._text_sha256(family_hypothesis)
        family_id = family_hypothesis_sha256[:16]
        protocol_version = str(
            self.variant_contract.get("protocol_version", "paper_family_v1")
        )
        accepted: dict[str, dict[str, Any]] = {}
        accepted_names: list[str] = []
        accepted_expressions: list[str] = []
        accepted_references: list[dict[str, str]] = []

        for role in roles:
            role_id = str(role["id"]).strip()
            variation_axis = str(role.get("variation_axis", "")).strip()
            variant_hypothesis = self._build_variant_hypothesis(
                family_hypothesis,
                role,
            )
            role_prompt = self._build_variant_prompt(
                user_prompt,
                family_id,
                role,
                accepted_references,
            )
            role_system_prompt = (
                f"{system_prompt}\n\n"
                "PAPER-FAMILY SYSTEM OVERRIDE (highest priority):\n"
                f"- Generate exactly one factor for variant_id={role_id}.\n"
                "- Ignore every earlier instruction or example that requests "
                "two, three, or 2-3 factors.\n"
                "- The JSON object must contain exactly one top-level member, "
                "whose key is the factor name and whose value follows the "
                "existing factor schema.\n"
                "- Do not emit sibling variants, metadata, Markdown, or prose."
            )
            last_error = "no response"

            for attempt in range(1, self.variant_max_attempts + 1):
                try:
                    response, names, expressions = (
                        self._generate_candidate_response(
                            hypothesis=variant_hypothesis,
                            user_prompt=role_prompt,
                            system_prompt=role_system_prompt,
                            json_flag=json_flag,
                            context={
                                **context,
                                "target_hypothesis": variant_hypothesis,
                            },
                            expected_factor_count=1,
                            max_attempts_override=1,
                        )
                    )
                    response_dict = robust_json_parse(response)
                    factor_name = names[0]
                    expression = expressions[0]
                    factor_data = response_dict[factor_name]
                    if factor_name in accepted_names:
                        raise ValueError(
                            f"duplicate sibling factor name: {factor_name}"
                        )
                    if expression in accepted_expressions:
                        raise ValueError(
                            f"duplicate sibling expression: {expression}"
                        )
                    if not self._expression_uses_only_allowed_features(
                        expression
                    ):
                        raise ValueError(
                            "expression uses a raw feature outside the "
                            "variant contract"
                        )

                    prompt_material = (
                        f"{role_system_prompt}\n\0{role_prompt}"
                    )
                    model_name = (
                        LLM_SETTINGS.chat_model
                        if LLM_SETTINGS.structured_json_mode_strict
                        else (
                            LLM_SETTINGS.reasoning_model
                            or LLM_SETTINGS.chat_model
                        )
                    )
                    quality_gate = factor_data.setdefault(
                        "quality_gate",
                        {},
                    )
                    quality_gate["variant_contract"] = {
                        "protocolVersion": protocol_version,
                        "familyId": family_id,
                        "familyHypothesisSha256": (
                            family_hypothesis_sha256
                        ),
                        "variantId": role_id,
                        "variationAxis": variation_axis,
                        "variantHypothesis": variant_hypothesis,
                        "generationAttempt": attempt,
                        "model": model_name,
                        "promptSha256": self._text_sha256(prompt_material),
                        "responseSha256": self._text_sha256(response),
                    }
                    accepted[factor_name] = factor_data
                    accepted_names.append(factor_name)
                    accepted_expressions.append(expression)
                    accepted_references.append(
                        {
                            "variant_id": role_id,
                            "factor_name": factor_name,
                            "expression": expression,
                        }
                    )
                    break
                except (FactorEmptyError, KeyError, TypeError, ValueError) as exc:
                    last_error = str(exc)
                    logger.warning(
                        "Variant generation rejected: "
                        f"role={role_id}, attempt={attempt}/"
                        f"{self.variant_max_attempts}, reason={last_error}"
                    )
            else:
                raise FactorEmptyError(
                    "Variant family rejected after independent repair budget: "
                    f"role={role_id}, reason={last_error}"
                )

        if len(accepted) != self.factors_per_hypothesis:
            raise FactorEmptyError(
                "Variant family is incomplete: "
                f"expected {self.factors_per_hypothesis}, got {len(accepted)}"
            )
        return (
            json.dumps(accepted, ensure_ascii=False),
            accepted_names,
            accepted_expressions,
        )
        
    @property
    def quality_gate(self):
        """Lazy-load FactorQualityGate."""
        if self._quality_gate is None and self.consistency_enabled:
            try:
                from quantaalpha.factors.regulator.consistency_checker import (
                    ComplexityChecker,
                    FactorConsistencyChecker,
                    FactorQualityGate,
                    RedundancyChecker,
                )
                self._quality_gate = FactorQualityGate(
                    consistency_checker=FactorConsistencyChecker(
                        enabled=self.consistency_enabled,
                        strict_mode=self.consistency_strict_mode,
                        require_consistent=self.consistency_require_pass,
                        max_correction_attempts=self.max_correction_attempts,
                    ),
                    complexity_checker=ComplexityChecker(
                        enabled=self.complexity_enabled,
                        symbol_length_threshold=self.factor_regulator.symbol_length_threshold,
                        base_features_threshold=self.factor_regulator.base_features_threshold,
                        free_args_ratio_threshold=self.factor_regulator.free_args_ratio_threshold,
                        fail_closed=self.quality_gate_fail_closed_on_error,
                    ),
                    redundancy_checker=RedundancyChecker(
                        enabled=self.redundancy_enabled,
                        duplication_threshold=self.factor_regulator.duplication_threshold,
                        factor_zoo_path=self.factor_regulator.factor_zoo_path,
                        fail_closed=self.quality_gate_fail_closed_on_error,
                    ),
                    consistency_enabled=self.consistency_enabled,
                    complexity_enabled=self.complexity_enabled,
                    redundancy_enabled=self.redundancy_enabled,
                )
            except ImportError as e:
                logger.warning(f"Could not load consistency checker: {e}")
                self._quality_gate = None
        return self._quality_gate
        
    def prepare_context(self, hypothesis: Hypothesis, trace: Trace, history_limit: int = DEFAULT_HISTORY_LIMIT) -> Tuple[dict | bool]:
        scenario = trace.scen.get_scenario_all_desc()
        output_format_key = (
            "single_factor_experiment_output_format"
            if self._variant_contract_enabled()
            else "factor_experiment_output_format"
        )
        experiment_output_format = qa_prompt_dict[output_format_key]
        function_lib_description = qa_prompt_dict['function_lib_description']
        hypothesis_and_feedback = render_hypothesis_and_feedback(
            qa_prompt_dict, trace, history_limit
        )

        experiment_list: List[FactorExperiment] = [t[1] for t in trace.hist]

        factor_list = []
        for experiment in experiment_list:
            factor_list.extend(experiment.sub_tasks)

        return {
            "target_hypothesis": str(hypothesis),
            "scenario": scenario,
            "hypothesis_and_feedback": hypothesis_and_feedback,
            "function_lib_description": function_lib_description,
            "experiment_output_format": experiment_output_format,
            "target_list": factor_list,
            "RAG": None,
        }, True
        
    def convert(self, hypothesis: Hypothesis, trace: Trace) -> Experiment:
        """Convert hypothesis to factor expressions; supports dynamic history limit."""
        history_limit = DEFAULT_HISTORY_LIMIT
        
        while history_limit >= MIN_HISTORY_LIMIT:
            try:
                return self._convert_with_history_limit(hypothesis, trace, history_limit)
            except Exception as e:
                if is_input_length_error(str(e)) and history_limit > MIN_HISTORY_LIMIT:
                    history_limit -= 1
                    logger.warning(f"Input length exceeded, retrying with history_limit={history_limit}...")
                else:
                    raise
        
        # Last attempt with minimum history limit
        return self._convert_with_history_limit(hypothesis, trace, MIN_HISTORY_LIMIT)
    
    def _convert_with_history_limit(self, hypothesis: Hypothesis, trace: Trace, history_limit: int) -> Experiment:
        """Convert with given history limit."""
        context, json_flag = self.prepare_context(hypothesis, trace, history_limit)
        generation_count = (
            1
            if self._variant_contract_enabled()
            else self.factors_per_hypothesis
        )
        system_prompt = (
            Environment(undefined=StrictUndefined)
            .from_string(qa_prompt_dict["hypothesis2experiment"]["system_prompt"])
            .render(
                targets=self.targets,
                scenario=trace.scen.background, # get_scenario_all_desc(filtered_tag="hypothesis_and_experiment"),
                experiment_output_format=context["experiment_output_format"],
                factor_count=generation_count,
            )
        )
        user_prompt = (
            Environment(undefined=StrictUndefined)
            .from_string(qa_prompt_dict["hypothesis2experiment"]["user_prompt"])
            .render(
                targets=self.targets,
                target_hypothesis=context["target_hypothesis"],
                hypothesis_and_feedback=context["hypothesis_and_feedback"],
                function_lib_description=context["function_lib_description"],
                target_list=context["target_list"],
                RAG=context["RAG"], 
                expression_duplication=None
            )
        )
        user_prompt = self._append_factor_count_instruction(
            user_prompt,
            generation_count,
        )
        
        if self._variant_contract_enabled():
            resp, proposed_names, proposed_exprs = (
                self._generate_variant_family_response(
                    hypothesis=hypothesis,
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    json_flag=json_flag,
                    context=context,
                )
            )
        else:
            resp, proposed_names, proposed_exprs = (
                self._generate_candidate_response(
                    hypothesis=hypothesis,
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    json_flag=json_flag,
                    context=context,
                )
            )

        # Add valid factors to the factor regulator
        self.factor_regulator.add_factor(proposed_names, proposed_exprs)
                
                
        return self.convert_response(resp, trace)
    

    def convert_response(self, response: str, trace: Trace) -> FactorExperiment:
        response_dict = self._limit_response_dict(robust_json_parse(response))
        tasks = []

        for factor_name in response_dict:
            factor_data = response_dict.get(factor_name, {})
            if not isinstance(factor_data, dict):
                continue
            description = factor_data.get("description", "")
            formulation = factor_data.get("formulation", "")
            expression = factor_data.get("expression", "")
            variables = factor_data.get("variables", {})
            tasks.append(
                FactorTask(
                    factor_name=factor_name,
                    factor_description=description,
                    factor_formulation=formulation,
                    factor_expression=expression,
                    variables=variables,
                    quality_gate=factor_data.get("quality_gate") or {},
                )
            )
            
        exp = QlibFactorExperiment(tasks)
        exp.based_experiments = [QlibFactorExperiment(sub_tasks=[])] + [t[1] for t in trace.hist if t[2]]

        unique_tasks = []

        for task in tasks:
            duplicate = False
            for based_exp in exp.based_experiments:
                for sub_task in based_exp.sub_tasks:
                    if task.factor_name == sub_task.factor_name:
                        duplicate = True
                        break
                if duplicate:
                    break
            if not duplicate:
                unique_tasks.append(task)

        exp.tasks = unique_tasks
        return exp



class BacktestHypothesis2FactorExpression(FactorHypothesis2Experiment):
    def __init__(self, factor_path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.factor_path = factor_path
        
    def convert_response(self, *args, **kwargs) -> FactorExperiment:
        return super().convert_response(*args, **kwargs)
        
    def prepare_context(self, *args, **kwargs) -> Tuple[dict | bool]:
        return super().prepare_context(*args, **kwargs)
        
    def convert(self, hypothesis: Hypothesis, trace: Trace) -> FactorExperiment:
        if os.path.exists(self.factor_path):
            tasks = []
            factor_df = pd.read_csv(self.factor_path, usecols=["factor_name", "factor_expression"], index_col=None)
            for index, row in factor_df.iterrows():
                tasks.append(
                    FactorTask(
                        factor_name=row["factor_name"],
                        factor_description="",
                        factor_formulation="",
                        factor_expression=row["factor_expression"],
                        variables="",
                    )
                )
            
            exp = QlibFactorExperiment(tasks)
            exp.based_experiments = [QlibFactorExperiment(sub_tasks=[])] + [t[1] for t in trace.hist if t[2]]

            unique_tasks = []

            for task in tasks:
                duplicate = False
                for based_exp in exp.based_experiments:
                    for sub_task in based_exp.sub_tasks:
                        if task.factor_name == sub_task.factor_name:
                            duplicate = True
                            break
                    if duplicate:
                        break
                if not duplicate:
                    unique_tasks.append(task)

            exp.tasks = unique_tasks
            return exp
            
        else:
            raise ValueError(f"File {self.factor_csv_path} does not exist. ")
        
    
