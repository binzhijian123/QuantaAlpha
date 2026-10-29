#!/usr/bin/env python3
"""
Backtest runner using Qlib: load factors (official/custom), compute custom factor values, train, backtest, evaluate.
Modes: official (Qlib DataLoader) or custom (expr_parser + function_lib).
"""

import json
import hashlib
import logging
import math
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd
import yaml

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from quantaalpha.utils.qlib_provider import (
    provider_contract_fingerprint,
    select_provider_for_config,
)

logger = logging.getLogger(__name__)


def normalize_market_index(df: pd.DataFrame, df_name: str = "data") -> pd.DataFrame:
    """Return a copy indexed by (datetime, instrument)."""
    if not isinstance(df.index, pd.MultiIndex) or df.index.nlevels != 2:
        raise ValueError(f"{df_name} must use a two-level MultiIndex")

    names = [str(name).lower() if name is not None else None for name in df.index.names]
    datetime_level = next((i for i, name in enumerate(names) if name in {"datetime", "date"}), None)
    instrument_level = next((i for i, name in enumerate(names) if name in {"instrument", "stock"}), None)

    if datetime_level is None:
        candidates = []
        for i in range(2):
            values = df.index.get_level_values(i)
            if pd.api.types.is_datetime64_any_dtype(values):
                candidates.append(i)
                continue
            if pd.api.types.is_string_dtype(values):
                sample = pd.Series(values[: min(len(values), 20)]).astype(str)
                if not sample.str.match(r"^\d{4}-\d{1,2}-\d{1,2}").all():
                    continue
            parsed = pd.to_datetime(values, errors="coerce")
            if len(parsed) and float(parsed.notna().mean()) == 1.0:
                candidates.append(i)
        if len(candidates) != 1:
            raise ValueError(
                f"Cannot identify datetime level for {df_name}; names={df.index.names}, "
                f"dtypes={[str(df.index.get_level_values(i).dtype) for i in range(2)]}"
            )
        datetime_level = candidates[0]

    if instrument_level is None:
        instrument_level = 1 - datetime_level
    if instrument_level == datetime_level:
        raise ValueError(f"Invalid index levels for {df_name}: {df.index.names}")

    datetimes = pd.to_datetime(df.index.get_level_values(datetime_level), errors="coerce")
    if datetimes.isna().any():
        raise ValueError(f"{df_name} contains invalid datetime index values")
    instruments = df.index.get_level_values(instrument_level).astype(str)
    if (instruments == "").any():
        raise ValueError(f"{df_name} contains empty instrument index values")

    normalized = df.copy()
    normalized.index = pd.MultiIndex.from_arrays(
        [datetimes, instruments], names=["datetime", "instrument"]
    )
    if normalized.index.has_duplicates:
        raise ValueError(f"{df_name} contains duplicate (datetime, instrument) rows")
    return normalized.sort_index()


def filter_market_frame_by_selector(
    frame: pd.DataFrame, selector: Any
) -> pd.DataFrame:
    """Filter a market frame for Qlib's date-range selector formats."""
    if selector is None:
        return frame

    start = end = None
    if isinstance(selector, slice):
        start, end = selector.start, selector.stop
    elif isinstance(selector, (list, tuple)) and len(selector) == 2:
        start, end = selector
    else:
        return frame

    try:
        dates = frame.index.get_level_values("datetime")
    except KeyError:
        dates = frame.index.get_level_values(0)

    mask = np.ones(len(frame), dtype=bool)
    if start is not None:
        mask &= dates >= pd.Timestamp(start)
    if end is not None:
        mask &= dates <= pd.Timestamp(end)
    return frame.loc[mask]


def cross_sectional_rank_normalize(
    frame: pd.DataFrame, columns: List[str], datetime_level: Any
) -> pd.DataFrame:
    """Match Qlib CSRankNorm for precomputed feature and label columns."""
    for column in columns:
        ranks = frame.groupby(level=datetime_level)[column].rank(pct=True)
        frame[column] = (ranks - 0.5) * 3.46
    return frame


def validate_dataset_segment_boundaries(dataset, segments: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Fail fast when a dataset handler leaks rows outside configured segments."""
    summary: Dict[str, Dict[str, Any]] = {}
    observed_dates: Dict[str, set] = {}
    configured_ranges: Dict[str, Tuple[pd.Timestamp, pd.Timestamp]] = {}

    for name, bounds in segments.items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            continue
        start, end = pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1])
        frame = dataset.prepare(name, col_set="label")
        if frame is None or len(frame) == 0:
            raise ValueError(f"Dataset segment {name!r} is empty")
        try:
            dates = pd.DatetimeIndex(frame.index.get_level_values("datetime"))
        except KeyError:
            dates = pd.DatetimeIndex(frame.index.get_level_values(0))
        actual_start, actual_end = dates.min(), dates.max()
        if actual_start < start or actual_end > end:
            raise ValueError(
                f"Dataset segment {name!r} contains rows outside configured range "
                f"[{start.date()}, {end.date()}]: observed "
                f"[{actual_start.date()}, {actual_end.date()}]"
            )
        unique_dates = set(dates.unique())
        observed_dates[name] = unique_dates
        configured_ranges[name] = (start, end)
        summary[name] = {
            "rows": int(len(frame)),
            "start": str(actual_start.date()),
            "end": str(actual_end.date()),
        }

    names = list(observed_dates)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            left_range = configured_ranges[left]
            right_range = configured_ranges[right]
            configured_disjoint = (
                left_range[1] < right_range[0] or right_range[1] < left_range[0]
            )
            if configured_disjoint and observed_dates[left].intersection(observed_dates[right]):
                raise ValueError(
                    f"Dataset segments {left!r} and {right!r} overlap despite disjoint configuration"
                )
    return summary


class BacktestRunner:
    """Backtest executor."""

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self._qlib_initialized = False

    def _load_config(self) -> Dict:
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        logger.info(f"Loaded config: {self.config_path}")
        return config

    def _init_qlib(self):
        if self._qlib_initialized:
            return
        import qlib
        provider_uri = select_provider_for_config(self.config, self.config_path.name)
        self.config['data']['provider_uri'] = str(provider_uri)
        region = self.config['data'].get('region', 'cn')
        qlib.init(provider_uri=str(provider_uri), region=region)
        self._qlib_initialized = True
        logger.info(f"Qlib initialized: {provider_uri} (region={region})")

    def run(self,
            factor_source: Optional[str] = None,
            factor_json: Optional[List[str]] = None,
            experiment_name: Optional[str] = None,
            output_name: Optional[str] = None,
            skip_uncached: bool = False,
            strategy_snapshot: Optional[str] = None,
            validation_decision: Optional[str] = None) -> Dict:
        """Run full backtest; returns metrics dict."""
        start_time_total = time.time()
        self._init_qlib()
        if factor_source:
            self.config['factor_source']['type'] = factor_source
        if factor_json:
            self.config['factor_source']['custom']['json_files'] = factor_json
        
        if output_name is None and factor_json:
            output_name = Path(factor_json[0]).stem

        exp_name = experiment_name or output_name or self.config['experiment']['name']
        rec_name = self.config['experiment']['recorder']

        print(f"\n{'='*50}")
        src = factor_json[0] if factor_json else exp_name
        print(f"Starting backtest: {src}")
        print(f"{'='*50}")

        factor_expressions, custom_factors = self._load_factors()
        print(f"[1/4] Loaded factors: Qlib {len(factor_expressions)}, custom {len(custom_factors)}")
        self._validate_strict_custom_factor_contract(custom_factors)

        computed_factors = None
        if custom_factors:
            computed_factors = self._compute_custom_factors(custom_factors, skip_compute=skip_uncached)
            n_computed = len(computed_factors.columns) if computed_factors is not None and not computed_factors.empty else 0
            print(f"[2/4] Computed custom factors: {n_computed}")
            if self.config.get('factor_source', {}).get('custom', {}).get('strict', False):
                expected_names = [factor['factor_name'] for factor in custom_factors]
                actual_names = list(computed_factors.columns) if computed_factors is not None else []
                if actual_names != expected_names:
                    raise RuntimeError(
                        "Strict custom factor computation mismatch: "
                        f"expected {expected_names}, computed {actual_names}"
                    )
        else:
            logger.debug("[2/4] No custom factors, skip")

        dataset = self._create_dataset(factor_expressions, computed_factors)
        segment_summary = validate_dataset_segment_boundaries(
            dataset, self.config['dataset']['segments']
        )
        logger.info("Validated dataset segments: %s", segment_summary)
        print("[3/4] Dataset created")

        metrics = self._train_and_backtest(dataset, exp_name, rec_name, output_name=output_name)
        if strategy_snapshot:
            self._save_strategy_snapshot(
                Path(strategy_snapshot),
                dataset,
                metrics,
                self._strategy_factor_metadata(factor_expressions, custom_factors),
                factor_source or self.config['factor_source']['type'],
                factor_json or [],
                validation_decision,
            )
        total_time = time.time() - start_time_total
        self._print_results(metrics, total_time)
        self._save_results(metrics, exp_name, factor_source or self.config['factor_source']['type'], 
                          len(factor_expressions) + len(custom_factors), total_time,
                          output_name=output_name)
        
        return metrics

    def train_only(
        self,
        *,
        strategy_snapshot: str,
        shadow_declaration: str,
        arm: str,
        factor_source: Optional[str] = None,
        factor_json: Optional[List[str]] = None,
        skip_uncached: bool = False,
    ) -> Dict[str, Any]:
        """Fit and freeze one research-shadow arm without evaluating returns."""
        from qlib.contrib.model.gbdt import LGBModel

        snapshot_path = Path(strategy_snapshot).expanduser().resolve()
        declaration_path = Path(shadow_declaration).expanduser().resolve()
        if snapshot_path.exists():
            raise FileExistsError(
                f"refusing to overwrite immutable strategy snapshot: {snapshot_path}"
            )
        declaration = self._validate_research_shadow_declaration(
            declaration_path, arm
        )

        self._init_qlib()
        if factor_source:
            self.config['factor_source']['type'] = factor_source
        if factor_json is not None:
            self.config['factor_source']['custom']['json_files'] = factor_json

        provider = Path(self.config['data']['provider_uri']).expanduser().resolve()
        market = str(self.config['data'].get('market') or 'csi300')
        provider_contract = provider_contract_fingerprint(provider, market)
        if provider_contract['fingerprint'] != declaration['providerFingerprint']:
            raise ValueError("research-shadow declaration Provider fingerprint mismatch")

        expected_arm = declaration['arms'][arm]
        effective_factor_source = self.config['factor_source']['type']
        if effective_factor_source != expected_arm['factorSource']:
            raise ValueError(
                f"research-shadow arm {arm} requires factor source "
                f"{expected_arm['factorSource']}"
            )
        resolved_factor_json = [
            str(Path(path).expanduser().resolve()) for path in (factor_json or [])
        ]
        expected_factor_json = [
            str(Path(path).expanduser().resolve())
            for path in expected_arm.get('factorJson', [])
        ]
        if resolved_factor_json != expected_factor_json:
            raise ValueError(f"research-shadow arm {arm} factor library mismatch")

        factor_expressions, custom_factors = self._load_factors()
        if effective_factor_source in {'custom', 'combined'}:
            self._validate_strict_custom_factor_contract(custom_factors)
        computed_factors = None
        if custom_factors:
            computed_factors = self._compute_custom_factors(
                custom_factors, skip_compute=skip_uncached
            )
            expected_names = [factor['factor_name'] for factor in custom_factors]
            actual_names = list(computed_factors.columns) if computed_factors is not None else []
            if actual_names != expected_names:
                raise RuntimeError(
                    "Strict research-shadow factor computation mismatch: "
                    f"expected {expected_names}, computed {actual_names}"
                )

        dataset = self._create_dataset(factor_expressions, computed_factors)
        segment_summary = validate_dataset_segment_boundaries(
            dataset, self.config['dataset']['segments']
        )
        model_config = self.config['model']
        if model_config['type'] != 'lgb':
            raise ValueError(f"Unsupported model type: {model_config['type']}")
        model = LGBModel(**model_config['params'])
        model.fit(dataset)
        self._trained_model = model

        factor_contracts = []
        for path in resolved_factor_json:
            factor_path = Path(path)
            factor_contracts.append(
                {
                    "path": path,
                    "sha256": self._file_sha256(factor_path),
                }
            )
        decision = declaration['historicalDecision']
        payload = {
            "version": 1,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "featureColumns": list(dataset.handler.get_cols("feature")),
            "factors": self._strategy_factor_metadata(
                factor_expressions, custom_factors
            ),
            "factorSource": effective_factor_source,
            "factorJson": resolved_factor_json,
            "factorJsonContracts": factor_contracts,
            "modelConfig": model_config,
            "trainingSegments": self.config['dataset']['segments'],
            "segmentSummary": segment_summary,
            "providerPath": str(provider),
            "providerContract": provider_contract,
            "backtestMetrics": {},
            "profitValidation": {
                "status": "RESEARCH_ONLY",
                "historicalStatus": "REJECTED",
                "historicalRunId": decision['runId'],
                "decisionPath": decision['path'],
                "decisionSha256": decision['sha256'],
                "reasons": decision.get('reasons', []),
            },
            "executionContract": self._paper_execution_contract(),
            "usageRealm": "RESEARCH_SHADOW",
            "runId": declaration['runId'],
            "arm": arm,
            "paperEligible": False,
            "automaticPromotionAllowed": False,
            "declarationPath": str(declaration_path),
            "declarationSha256": self._file_sha256(declaration_path),
            "providerLineageProofDir": str(
                Path(declaration['providerLineageProofDir']).expanduser().resolve()
            ),
        }
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
        with temporary_path.open("wb") as file:
            pickle.dump(payload, file)
        temporary_path.replace(snapshot_path)
        snapshot_sha = self._file_sha256(snapshot_path)
        snapshot_path.with_suffix(snapshot_path.suffix + ".sha256").write_text(
            snapshot_sha + "\n", encoding="ascii"
        )
        return {
            "arm": arm,
            "snapshotPath": str(snapshot_path),
            "snapshotSha256": snapshot_sha,
            "featureCount": len(payload['featureColumns']),
            "factorCount": len(payload['factors']),
            "segmentSummary": segment_summary,
            "providerFingerprint": provider_contract['fingerprint'],
            "returnsEvaluated": False,
        }

    @staticmethod
    def _strategy_factor_metadata(
        factor_expressions: Dict[str, str], custom_factors: List[Dict]
    ) -> List[Dict[str, Any]]:
        return [
            {"name": name, "expression": expression, "source": "qlib"}
            for name, expression in factor_expressions.items()
        ] + [
            {
                "name": factor.get("factor_name", factor.get("factor_id", "unknown")),
                "expression": factor.get("factor_expression", ""),
                "source": "custom",
                "factorId": factor.get("factor_id"),
                "feedbackDecision": factor.get("feedback_decision"),
            }
            for factor in custom_factors
        ]

    def _validate_research_shadow_declaration(
        self, declaration_path: Path, arm: str
    ) -> Dict[str, Any]:
        declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
        if declaration.get('usageRealm') != 'RESEARCH_SHADOW':
            raise ValueError("invalid research-shadow usage realm")
        if declaration.get('paperEligible') is not False:
            raise ValueError("research-shadow declaration cannot be paper eligible")
        if declaration.get('automaticPromotionAllowed') is not False:
            raise ValueError("research-shadow declaration cannot allow automatic promotion")
        if declaration.get('historicalStatus') != 'REJECTED':
            raise ValueError("research-shadow declaration must preserve REJECTED history")
        if arm not in (declaration.get('arms') or {}):
            raise ValueError(f"research-shadow declaration does not define arm {arm}")

        config_contract = declaration.get('config') or {}
        if Path(str(config_contract.get('path') or '')).expanduser().resolve() != self.config_path.resolve():
            raise ValueError("research-shadow declaration config path mismatch")
        if config_contract.get('sha256') != self._file_sha256(self.config_path):
            raise ValueError("research-shadow declaration config hash mismatch")

        decision = declaration.get('historicalDecision') or {}
        decision_path = Path(str(decision.get('path') or '')).expanduser().resolve()
        if not decision_path.is_file() or decision.get('sha256') != self._file_sha256(decision_path):
            raise ValueError("research-shadow historical decision hash mismatch")
        decision_payload = json.loads(decision_path.read_text(encoding="utf-8"))
        if decision_payload.get('status') != 'REJECTED':
            raise ValueError("research-shadow historical decision is no longer REJECTED")
        if decision_payload.get('runId') != decision.get('runId'):
            raise ValueError("research-shadow historical run ID mismatch")
        return declaration

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _save_strategy_snapshot(
        self,
        snapshot_path: Path,
        dataset,
        metrics: Dict,
        factors: List[Dict[str, str]],
        factor_source: str,
        factor_json: List[str],
        validation_decision: Optional[str],
    ) -> None:
        model = getattr(self, "_trained_model", None)
        if model is None:
            raise RuntimeError("trained model is unavailable for strategy snapshot")
        feature_columns = list(dataset.handler.get_cols("feature"))
        profit_validation = self._load_profit_validation(validation_decision)
        payload = {
            "version": 1,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "featureColumns": feature_columns,
            "factors": factors,
            "factorSource": factor_source,
            "factorJson": [str(Path(path).resolve()) for path in factor_json],
            "modelConfig": self.config["model"],
            "trainingSegments": self.config["dataset"]["segments"],
            "providerPath": self.config["data"]["provider_uri"],
            "backtestMetrics": metrics,
            "profitValidation": profit_validation,
            "executionContract": self._paper_execution_contract(),
        }
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
        with temporary_path.open("wb") as file:
            pickle.dump(payload, file)
        temporary_path.replace(snapshot_path)
        logger.info("Paper strategy snapshot saved: %s", snapshot_path)

    def _paper_execution_contract(self) -> Dict[str, Any]:
        strategy = ((self.config.get("backtest") or {}).get("strategy") or {})
        strategy_class = str(strategy.get("class") or "")
        kwargs = dict(strategy.get("kwargs") or {})
        if strategy_class != "TopkDropoutStrategy":
            raise ValueError(
                f"paper snapshot does not support strategy class: {strategy_class or 'missing'}"
            )
        method_buy = str(kwargs.get("method_buy", "top"))
        method_sell = str(kwargs.get("method_sell", "bottom"))
        if method_buy != "top" or method_sell != "bottom":
            raise ValueError(
                "paper snapshot requires deterministic TopkDropout method_buy=top and method_sell=bottom"
            )
        top_k = int(kwargs.get("topk", 0))
        n_drop = int(kwargs.get("n_drop", -1))
        risk_degree = float(kwargs.get("risk_degree", 0.95))
        if top_k < 1 or n_drop < 1 or n_drop > top_k:
            raise ValueError("invalid TopkDropout topk/n_drop execution contract")
        if not 0 < risk_degree <= 1:
            raise ValueError("invalid TopkDropout risk_degree execution contract")
        raw_hold_threshold = kwargs.get("hold_thresh", 1)
        try:
            hold_threshold = int(raw_hold_threshold)
            numeric_hold_threshold = float(raw_hold_threshold)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "paper snapshot requires an integer TopkDropout hold_thresh"
            ) from exc
        if (
            isinstance(raw_hold_threshold, bool)
            or not math.isfinite(numeric_hold_threshold)
            or numeric_hold_threshold != hold_threshold
        ):
            raise ValueError(
                "paper snapshot requires an integer TopkDropout hold_thresh"
            )
        only_tradable = bool(kwargs.get("only_tradable", False))
        forbid_all_trade_at_limit = bool(
            kwargs.get("forbid_all_trade_at_limit", True)
        )
        if hold_threshold < 1:
            raise ValueError("paper snapshot requires a positive TopkDropout hold_thresh")
        if only_tradable:
            raise ValueError("paper snapshot does not support TopkDropout only_tradable=true")
        if not forbid_all_trade_at_limit:
            raise ValueError(
                "paper snapshot requires TopkDropout forbid_all_trade_at_limit=true"
            )
        return {
            "version": 1 if hold_threshold == 1 else 2,
            "type": "topk_dropout",
            "strategyClass": strategy_class,
            "topK": top_k,
            "nDrop": n_drop,
            "riskDegree": risk_degree,
            "methodBuy": method_buy,
            "methodSell": method_sell,
            "holdThreshold": hold_threshold,
            "onlyTradable": only_tradable,
            "forbidAllTradeAtLimit": forbid_all_trade_at_limit,
        }

    @staticmethod
    def _load_profit_validation(decision_path: Optional[str]) -> Dict[str, Any]:
        if not decision_path:
            return {
                "status": "RESEARCH_ONLY",
                "reasons": ["No profit-validation decision was attached to this snapshot."],
            }

        path = Path(decision_path).resolve()
        with path.open("r", encoding="utf-8") as file:
            decision = json.load(file)
        status = decision.get("status")
        if status not in {"PAPER_CANDIDATE", "PAPER_VALIDATED"}:
            raise ValueError(
                "strategy snapshot requires a PAPER_CANDIDATE or PAPER_VALIDATED decision"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "status": status,
            "runId": decision.get("runId"),
            "protocolVersion": decision.get("protocolVersion"),
            "decisionPath": str(path),
            "decisionSha256": digest,
            "reasons": decision.get("reasons", []),
        }
    
    def _load_factors(self) -> Tuple[Dict[str, str], List[Dict]]:
        from .factor_loader import FactorLoader
        
        loader = FactorLoader(self.config)
        return loader.load_factors()

    def _validate_strict_custom_factor_contract(self, factors: List[Dict]) -> None:
        custom_config = self.config.get('factor_source', {}).get('custom', {})
        if not custom_config.get('strict', False):
            return

        expected_ids = custom_config.get('expected_factor_ids')
        actual_ids = [factor.get('factor_id') for factor in factors]
        if expected_ids is not None and actual_ids != expected_ids:
            raise ValueError(
                f"Strict custom factor IDs mismatch: expected {expected_ids}, loaded {actual_ids}"
            )

        provider_fingerprint = str(self.config.get('data', {}).get('provider_fingerprint') or '')
        market = str(self.config.get('data', {}).get('market') or '')
        if not provider_fingerprint:
            raise ValueError("Strict custom factor validation requires provider_fingerprint")
        for factor in factors:
            factor_id = str(factor.get('factor_id') or '')
            expression = str(factor.get('factor_expression') or '')
            cache = factor.get('cache_location') or {}
            cache_path = Path(str(cache.get('result_h5_path') or '')).expanduser()
            expected_cache_sha = str(cache.get('result_h5_sha256') or '')
            expected_expression_sha = hashlib.sha256(expression.encode('utf-8')).hexdigest()
            if (
                not cache_path.is_file()
                or not expected_cache_sha
                or cache.get('provider_fingerprint') != provider_fingerprint
                or cache.get('market') != market
                or cache.get('expression_sha256') != expected_expression_sha
            ):
                raise ValueError(
                    f"Strict custom factor cache contract is invalid: {factor_id}"
                )
            digest = hashlib.sha256()
            with cache_path.open('rb') as cache_file:
                for chunk in iter(lambda: cache_file.read(1024 * 1024), b''):
                    digest.update(chunk)
            actual_cache_sha = digest.hexdigest()
            if actual_cache_sha != expected_cache_sha:
                raise ValueError(
                    f"Strict custom factor cache hash mismatch: {factor_id}"
                )
    
    def _compute_custom_factors(self, factors: List[Dict], skip_compute: bool = False) -> Optional[pd.DataFrame]:
        """Compute custom factors (expr_parser + function_lib); supports cache; loads stock data only when needed."""
        from .custom_factor_calculator import CustomFactorCalculator
        from pathlib import Path

        llm_config = self.config.get('llm', {})
        cache_dir = llm_config.get('cache_dir')
        if cache_dir:
            cache_dir = Path(cache_dir)
        auto_extract = llm_config.get('auto_extract_cache', True)
        cache_namespace = str(llm_config.get('cache_namespace') or '')
        calculator = CustomFactorCalculator(
            data_df=None,
            cache_dir=cache_dir,
            auto_extract_cache=auto_extract,
            config=self.config,
            cache_namespace=cache_namespace,
        )
        result_df = calculator.calculate_factors_batch(factors, use_cache=True, skip_compute=skip_compute)
        if result_df is None:
            logger.error("Factor computation returned None")
            return None
        if not isinstance(result_df, pd.DataFrame):
            logger.error(f"Factor computation returned wrong type: {type(result_df)}")
            return None
        
        if result_df.empty:
            logger.error("Factor computation returned empty DataFrame")
            return None
        
        if not isinstance(result_df.index, pd.MultiIndex):
            logger.warning("Factor data index is not MultiIndex, attempting fix...")
        logger.debug(f"  Factor computation done: {len(result_df.columns)} factors, {len(result_df)} rows")
        
        return result_df
    
    def _create_dataset(self, 
                       factor_expressions: Dict[str, str],
                       computed_factors: Optional[pd.DataFrame] = None):
        """Create Qlib dataset (QlibDataLoader or precomputed factors + StaticDataLoader)."""
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandlerLP
        
        data_config = self.config['data']
        dataset_config = self.config['dataset']
        
        has_computed_factors = False
        if computed_factors is not None:
            if isinstance(computed_factors, pd.DataFrame):
                if len(computed_factors) > 0 and len(computed_factors.columns) > 0:
                    has_computed_factors = True
                    logger.debug(f"  Precomputed factors: {len(computed_factors.columns)} factors, {len(computed_factors)} rows")
                else:
                    logger.warning(f"  Precomputed factor DataFrame is empty: {computed_factors.shape}")
            else:
                logger.warning(f"  Precomputed factor type invalid: {type(computed_factors)}")
        
        # Prefer custom factor mode when computed factors exist
        if has_computed_factors:
            logger.debug("  Using custom factor mode (precomputed)")
            return self._create_dataset_with_computed_factors(
                factor_expressions, computed_factors
            )
        
        # Qlib-only factor mode
        expressions = list(factor_expressions.values())
        names = list(factor_expressions.keys())
        
        if not expressions:
            raise ValueError("No factor expressions available. If using custom factors, ensure factor computation succeeded.")
        
        handler_config = {
            'start_time': data_config['start_time'],
            'end_time': data_config['end_time'],
            'instruments': data_config['market'],
            'data_loader': {
                'class': 'QlibDataLoader',
                'module_path': 'qlib.contrib.data.loader',
                'kwargs': {
                    'config': {
                        'feature': (expressions, names),
                        'label': ([dataset_config['label']], ['LABEL0'])
                    }
                }
            },
            'learn_processors': dataset_config['learn_processors'],
            'infer_processors': dataset_config['infer_processors']
        }
        
        dataset = DatasetH(
            handler=DataHandlerLP(**handler_config),
            segments=dataset_config['segments']
        )
        
        logger.debug(f"  Qlib mode: {len(expressions)} factors, train={dataset_config['segments']['train']}")
        
        return dataset
    
    def _create_dataset_with_computed_factors(self,
                                              factor_expressions: Dict[str, str],
                                              computed_factors: pd.DataFrame):
        """Create dataset from precomputed factors: compute label, merge with factors, use custom DataHandler."""
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandler
        from qlib.data import D
        
        data_config = self.config['data']
        dataset_config = self.config['dataset']
        
        logger.debug(f"  Computed factor count: {len(computed_factors.columns)}")
        label_expr = dataset_config['label']
        label_df = self._compute_label(label_expr)
        
        all_feature_dfs = [normalize_market_index(computed_factors, "computed factors")]
        if factor_expressions:
            logger.debug(f"  Loading {len(factor_expressions)} Qlib-compatible factors")
            qlib_factors = self._load_qlib_factors(factor_expressions)
            if qlib_factors is not None and not qlib_factors.empty:
                all_feature_dfs.append(normalize_market_index(qlib_factors, "Qlib factors"))
        
        features_df = pd.concat(all_feature_dfs, axis=1)
        features_df = features_df.loc[:, ~features_df.columns.duplicated()]
        logger.debug(f"  Total factor count: {len(features_df.columns)}")

        features_df = normalize_market_index(features_df, "features")
        label_df = normalize_market_index(label_df, "label")
        
        common_index = features_df.index.intersection(label_df.index)
        if len(common_index) == 0 and len(features_df) > 0 and len(label_df) > 0:
            logger.warning("  Index intersection empty, aligning datetime types...")
            feat_dt = features_df.index.get_level_values('datetime')
            label_dt = label_df.index.get_level_values('datetime')
            logger.debug(f"  features datetime dtype={feat_dt.dtype}, sample={feat_dt[:3].tolist()}")
            logger.debug(f"  label    datetime dtype={label_dt.dtype}, sample={label_dt[:3].tolist()}")
            
            feat_inst = features_df.index.get_level_values('instrument')
            label_inst = label_df.index.get_level_values('instrument')
            logger.debug(f"  features instrument sample={feat_inst[:3].tolist()}")
            logger.debug(f"  label    instrument sample={label_inst[:3].tolist()}")
            
            common_index = features_df.index.intersection(label_df.index)
            logger.debug(f"  Intersection size after align: {len(common_index)}")

        if len(common_index) == 0:
            logger.warning("  Index intersection still empty, trying merge...")
            feat_reset = features_df.reset_index()
            label_reset = label_df.reset_index()
            dt_col = 'datetime' if 'datetime' in feat_reset.columns else feat_reset.columns[0]
            inst_col = 'instrument' if 'instrument' in feat_reset.columns else feat_reset.columns[1]
            
            merged = pd.merge(
                feat_reset, label_reset,
                on=[dt_col, inst_col],
                how='inner'
            )
            logger.debug(f"  Merged rows: {len(merged)}")
            if len(merged) == 0:
                raise ValueError(
                    f"Factor and label data could not be aligned. "
                    f"features: {len(features_df)} rows, index names={list(features_df.index.names)}; "
                    f"label: {len(label_df)} rows, index names={list(label_df.index.names)}"
                )
            
            merged = merged.set_index([dt_col, inst_col])
            merged.index.names = ['datetime', 'instrument']
            
            feature_cols = [c for c in features_df.columns if c in merged.columns]
            label_cols = [c for c in label_df.columns if c in merged.columns]
            features_df = merged[feature_cols]
            label_df = merged[label_cols]
        else:
            features_df = features_df.loc[common_index]
            label_df = label_df.loc[common_index]
        
        logger.debug(f"  Data rows: {len(features_df)}")
        if len(features_df) == 0:
            raise ValueError("No rows after index alignment; cannot run backtest")
        combined_df = pd.concat([features_df, label_df], axis=1)
        from qlib.data.dataset.processor import Fillna, ProcessInf, CSRankNorm, DropnaLabel
        feature_cols = list(features_df.columns)
        label_cols = list(label_df.columns)
        combined_df[feature_cols] = combined_df[feature_cols].fillna(0)
        dt_level = combined_df.index.names[0] if combined_df.index.names[0] else 0
        for col in feature_cols + label_cols:
            if np.isinf(combined_df[col].to_numpy()).any():
                combined_df[col] = combined_df.groupby(level=dt_level)[col].transform(
                    lambda values: values.replace(
                        [np.inf, -np.inf], values[~np.isinf(values)].mean()
                    )
                )
        combined_df = cross_sectional_rank_normalize(
            combined_df, feature_cols, dt_level
        )
        combined_df = combined_df.dropna(subset=label_cols)
        combined_df = cross_sectional_rank_normalize(
            combined_df, label_cols, dt_level
        )
        
        logger.debug(f"  Rows after preprocessing: {len(combined_df)}")
        feature_tuples = [('feature', col) for col in feature_cols]
        label_tuples = [('label', col) for col in label_cols]
        
        combined_df_multi = combined_df.copy()
        combined_df_multi.columns = pd.MultiIndex.from_tuples(
            feature_tuples + label_tuples
        )
        
        class PrecomputedDataHandler(DataHandler):
            """DataHandler for precomputed data."""
            
            def __init__(self, data_df, segments):
                self._data = data_df
                self._segments = segments
            
            @property
            def data_loader(self):
                return None
            
            @property
            def instruments(self):
                try:
                    return list(self._data.index.get_level_values('instrument').unique())
                except KeyError:
                    return list(self._data.index.get_level_values(1).unique())
            
            def fetch(self, selector=None, level='datetime', col_set='feature',
                     data_key=None, squeeze=False, proc_func=None):
                if col_set in ('feature', 'label'):
                    result = self._data[col_set].copy()
                elif col_set == '__all' or col_set is None:
                    result = self._data.copy()
                else:
                    if isinstance(col_set, (list, tuple)):
                        result = self._data[list(col_set)].copy()
                    else:
                        result = self._data.copy()
                result = filter_market_frame_by_selector(result, selector)
                
                if squeeze and result.shape[1] == 1:
                    result = result.iloc[:, 0]
                
                return result
            
            def get_cols(self, col_set='feature'):
                if col_set in self._data.columns.get_level_values(0):
                    return list(self._data[col_set].columns)
                return list(self._data.columns.get_level_values(1))
            
            def setup_data(self, **kwargs):
                pass
            
            def config(self, **kwargs):
                pass
        
        handler = PrecomputedDataHandler(combined_df_multi, dataset_config['segments'])
        dataset = DatasetH(
            handler=handler,
            segments=dataset_config['segments']
        )
        
        logger.debug(f"  Custom factor mode: {len(feature_cols)} factors, {len(combined_df)} rows, train={dataset_config['segments']['train']}")
        
        return dataset
    
    def _compute_label(self, label_expr: str) -> pd.DataFrame:
        """Compute label using Qlib (label requires look-ahead)."""
        from qlib.data import D
        
        data_config = self.config['data']
        
        logger.debug(f"  Label expr: {label_expr}")
        
        stock_list = D.instruments(data_config['market'])
        
        label_df = D.features(
            stock_list,
            [label_expr],
            start_time=data_config['start_time'],
            end_time=data_config['end_time'],
            freq='day'
        )
        
        label_df.columns = ['LABEL0']
        
        logger.debug(f"  Label rows: {len(label_df)}")
        
        return label_df
    
    def _load_qlib_factors(self, factor_expressions: Dict[str, str]) -> Optional[pd.DataFrame]:
        """Load Qlib-compatible factors."""
        from qlib.data import D
        
        data_config = self.config['data']
        
        try:
            stock_list = D.instruments(data_config['market'])
            
            expressions = list(factor_expressions.values())
            names = list(factor_expressions.keys())
            
            df = D.features(
                stock_list,
                expressions,
                start_time=data_config['start_time'],
                end_time=data_config['end_time'],
                freq='day'
            )
            
            df.columns = names
            return df
        except Exception as e:
            logger.warning(f"Failed to load Qlib factors: {e}")
            return None
    
    def _train_and_backtest(self, dataset, exp_name: str, rec_name: str, output_name: Optional[str] = None) -> Dict:
        """Train model and run backtest."""
        from qlib.contrib.model.gbdt import LGBModel
        from qlib.data import D
        from qlib.workflow import R
        from qlib.workflow.record_temp import SignalRecord, SigAnaRecord
        from qlib.backtest import backtest as qlib_backtest
        from qlib.contrib.evaluate import risk_analysis
        
        model_config = self.config['model']
        backtest_config = self.config['backtest']['backtest']
        strategy_config = self.config['backtest']['strategy']
        
        metrics = {}
        
        with R.start(experiment_name=exp_name, recorder_name=rec_name):
            # Train model
            train_start = time.time()
            
            if model_config['type'] == 'lgb':
                model = LGBModel(**model_config['params'])
            else:
                raise ValueError(f"Unsupported model type: {model_config['type']}")
            
            model.fit(dataset)
            self._trained_model = model
            print(f"[4/4] Train LightGBM done ({time.time()-train_start:.1f}s)")
            
            # Generate prediction
            pred = model.predict(dataset)
            logger.debug(f"  Pred shape: {pred.shape}")
            
            # Save prediction
            sr = SignalRecord(recorder=R.get_recorder(), model=model, dataset=dataset)
            sr.generate()
            
            # Compute IC metrics
            try:
                sar = SigAnaRecord(recorder=R.get_recorder(), ana_long_short=False, ann_scaler=252)
                sar.generate()
                
                recorder = R.get_recorder()
                try:
                    ic_series = recorder.load_object("sig_analysis/ic.pkl")
                    ric_series = recorder.load_object("sig_analysis/ric.pkl")
                    
                    if isinstance(ic_series, pd.Series) and len(ic_series) > 0:
                        metrics['IC'] = float(ic_series.mean())
                        metrics['ICIR'] = float(ic_series.mean() / ic_series.std()) if ic_series.std() > 0 else 0.0
                    
                    if isinstance(ric_series, pd.Series) and len(ric_series) > 0:
                        metrics['Rank IC'] = float(ric_series.mean())
                        metrics['Rank ICIR'] = float(ric_series.mean() / ric_series.std()) if ric_series.std() > 0 else 0.0
                    
                    print(f"  IC={metrics.get('IC', 0):.6f}, ICIR={metrics.get('ICIR', 0):.6f}, "
                          f"Rank IC={metrics.get('Rank IC', 0):.6f}, Rank ICIR={metrics.get('Rank ICIR', 0):.6f}")
                except Exception as e:
                    logger.warning(f"Could not read IC result: {e}")
            except Exception as e:
                logger.warning(f"IC analysis failed: {e}")
            # Portfolio backtest
            try:
                bt_start = time.time()
                
                market = self.config['data']['market']
                instruments = D.instruments(market)
                stock_list = D.list_instruments(
                    instruments,
                    start_time=backtest_config['start_time'],
                    end_time=backtest_config['end_time'],
                    as_list=True
                )
                logger.debug(f"  Stock count: {len(stock_list)}")
                if len(stock_list) < 10:
                    logger.warning(f"Stock pool too small ({len(stock_list)}), results may be unreliable")
                # Filter invalid price signals
                try:
                    price_data = D.features(
                        stock_list,
                        ['$close'],
                        start_time=backtest_config['start_time'],
                        end_time=backtest_config['end_time'],
                        freq='day'
                    )
                    invalid_mask = (price_data['$close'] == 0) | (price_data['$close'].isna())
                    invalid_count = invalid_mask.sum()
                    
                    if invalid_count > 0:
                        logger.debug(f"  Found {invalid_count} zero/NaN price records")
                        if isinstance(pred, pd.Series):
                            invalid_indices = invalid_mask[invalid_mask].index
                            invalid_set = set()
                            for idx in invalid_indices:
                                instrument, datetime = idx
                                invalid_set.add((datetime, instrument))
                            
                            filtered_count = 0
                            for idx in pred.index:
                                if idx in invalid_set:
                                    pred.loc[idx] = np.nan
                                    filtered_count += 1
                            
                            if filtered_count > 0:
                                logger.debug(f"  Filtered {filtered_count} invalid price signals")
                except Exception as filter_err:
                    logger.warning(f"Price filter failed: {filter_err}")
                
                portfolio_metric_dict, indicator_dict = qlib_backtest(
                    executor={
                        "class": "SimulatorExecutor",
                        "module_path": "qlib.backtest.executor",
                        "kwargs": {
                            "time_per_step": "day",
                            "generate_portfolio_metrics": True,
                            "verbose": False,
                            "indicator_config": {"show_indicator": False}
                        }
                    },
                    strategy=self._qlib_strategy_spec(strategy_config, pred),
                    start_time=backtest_config['start_time'],
                    end_time=backtest_config['end_time'],
                    account=backtest_config['account'],
                    benchmark=backtest_config['benchmark'],
                    exchange_kwargs={
                        "codes": stock_list,
                        **backtest_config['exchange_kwargs']
                    }
                )
                
                print(f"  Portfolio backtest done ({time.time()-bt_start:.1f}s)")
                # Extract portfolio metrics
                if portfolio_metric_dict and "1day" in portfolio_metric_dict:
                    report_df, positions_df = portfolio_metric_dict["1day"]
                    position_counts = self._position_count_series(positions_df)
                    if position_counts.empty:
                        raise RuntimeError("Qlib backtest did not return auditable positions")
                    metrics["position_count_max"] = int(position_counts.max())
                    metrics["position_count_last"] = int(position_counts.iloc[-1])
                    metrics["position_count_mean"] = float(position_counts.mean())
                    output_dir = Path(
                        self.config['experiment'].get(
                            'output_dir', './backtest_v2_results'
                        )
                    )
                    output_dir.mkdir(parents=True, exist_ok=True)
                    file_prefix = output_name if output_name else exp_name
                    position_counts.rename("position_count").to_csv(
                        output_dir / f"{file_prefix}_position_counts.csv",
                        index_label="date",
                    )
                    
                    if isinstance(report_df, pd.DataFrame) and 'return' in report_df.columns:
                        portfolio_return = report_df['return'].replace([np.inf, -np.inf], np.nan).fillna(0)
                        bench_return = report_df['bench'].replace([np.inf, -np.inf], np.nan).fillna(0) if 'bench' in report_df.columns else 0
                        cost = report_df['cost'].replace([np.inf, -np.inf], np.nan).fillna(0) if 'cost' in report_df.columns else 0
                        
                        excess_return_with_cost = portfolio_return - bench_return - cost
                        excess_return_with_cost = excess_return_with_cost.dropna()
                        
                        if len(excess_return_with_cost) > 0:
                            try:
                                daily_df = report_df.copy()
                                daily_df['excess_return'] = excess_return_with_cost
                                
                                output_dir = Path(self.config['experiment'].get('output_dir', './backtest_v2_results'))
                                output_dir.mkdir(parents=True, exist_ok=True)
                                
                                file_prefix = output_name if output_name else exp_name
                                csv_path = output_dir / f"{file_prefix}_cumulative_excess.csv"
                                save_df = pd.DataFrame(index=daily_df.index)
                                save_df['portfolio_return'] = portfolio_return
                                save_df['benchmark_return'] = bench_return
                                save_df['cost'] = cost
                                save_df['daily_excess_return'] = excess_return_with_cost
                                save_df['cumulative_excess_return'] = save_df['daily_excess_return'].cumsum()
                                
                                save_df.index.name = 'date'
                                save_df.to_csv(csv_path)
                                logger.debug(f"  Daily excess return saved: {csv_path}")
                            except Exception as csv_err:
                                logger.warning(f"Failed to save daily CSV: {csv_err}")

                            analysis = risk_analysis(excess_return_with_cost)
                            
                            if isinstance(analysis, pd.DataFrame):
                                analysis = analysis['risk'] if 'risk' in analysis.columns else analysis.iloc[:, 0]
                            
                            ann_ret = float(analysis.get('annualized_return', 0))
                            info_ratio = float(analysis.get('information_ratio', 0))
                            max_dd = float(analysis.get('max_drawdown', 0))
                            
                            if not np.isnan(ann_ret) and not np.isinf(ann_ret):
                                metrics['annualized_return'] = ann_ret
                            if not np.isnan(info_ratio) and not np.isinf(info_ratio):
                                metrics['information_ratio'] = info_ratio
                            if not np.isnan(max_dd) and not np.isinf(max_dd):
                                metrics['max_drawdown'] = max_dd
                            
                            if max_dd != 0 and not np.isnan(ann_ret) and not np.isinf(ann_ret):
                                calmar = ann_ret / abs(max_dd)
                                if not np.isnan(calmar) and not np.isinf(calmar):
                                    metrics['calmar_ratio'] = calmar
                            
            except Exception as e:
                raise RuntimeError(f"Portfolio backtest failed: {e}") from e

            required_metrics = {"annualized_return", "max_drawdown", "information_ratio"}
            missing_metrics = sorted(required_metrics - set(metrics))
            if missing_metrics:
                raise RuntimeError(
                    "Portfolio backtest did not produce required metrics: "
                    + ", ".join(missing_metrics)
                )
        
        return metrics

    @staticmethod
    def _qlib_strategy_spec(strategy_config: Dict[str, Any], pred: Any) -> Dict[str, Any]:
        """Bind the realized signal without dropping frozen strategy parameters."""
        kwargs = dict(strategy_config.get("kwargs") or {})
        kwargs["signal"] = BacktestRunner._deterministic_strategy_signal(pred)
        return {
            "class": strategy_config["class"],
            "module_path": strategy_config["module_path"],
            "kwargs": kwargs,
        }

    @staticmethod
    def _deterministic_strategy_signal(signal: Any) -> Any:
        """Preserve score order while breaking daily ties by instrument code."""
        if isinstance(signal, pd.Series):
            scores = signal
            as_series = True
        elif isinstance(signal, pd.DataFrame):
            if signal.shape[1] != 1:
                raise RuntimeError("Qlib strategy signal must contain exactly one column")
            scores = signal.iloc[:, 0]
            as_series = False
        else:
            return signal

        if not isinstance(scores.index, pd.MultiIndex):
            raise RuntimeError("Qlib strategy signal must use a MultiIndex")
        names = list(scores.index.names)
        if "datetime" not in names or "instrument" not in names:
            raise RuntimeError(
                "Qlib strategy signal index must contain datetime and instrument levels"
            )
        if scores.index.has_duplicates:
            raise RuntimeError("Qlib strategy signal contains duplicate market rows")

        numeric_scores = pd.to_numeric(scores, errors="coerce")
        invalid_numeric = scores.notna() & numeric_scores.isna()
        if invalid_numeric.any():
            raise RuntimeError("Qlib strategy signal contains non-numeric scores")

        work = pd.DataFrame(
            {
                "_row": np.arange(len(scores), dtype=np.int64),
                "_datetime": pd.to_datetime(
                    scores.index.get_level_values("datetime"), errors="coerce"
                ),
                "_instrument": scores.index.get_level_values("instrument").astype(str),
                "_score": numeric_scores.to_numpy(dtype=float),
            }
        )
        if work["_datetime"].isna().any() or (work["_instrument"] == "").any():
            raise RuntimeError("Qlib strategy signal contains invalid market index values")

        valid = work[work["_score"].notna()].sort_values(
            ["_datetime", "_score", "_instrument"],
            ascending=[True, False, True],
            kind="mergesort",
        )
        valid["_strategy_score"] = -valid.groupby(
            "_datetime", sort=False
        ).cumcount().astype(float)
        ranked_values = np.full(len(scores), np.nan, dtype=float)
        ranked_values[valid["_row"].to_numpy(dtype=np.int64)] = valid[
            "_strategy_score"
        ].to_numpy(dtype=float)

        ranked = pd.Series(ranked_values, index=scores.index, name=scores.name)
        if as_series:
            return ranked
        result = signal.copy()
        result.iloc[:, 0] = ranked_values
        return result

    @staticmethod
    def _position_count_series(positions: Any) -> pd.Series:
        if not isinstance(positions, Mapping):
            raise RuntimeError("Qlib positions payload is not a date mapping")
        counts: dict[pd.Timestamp, int] = {}
        for trade_date, position in positions.items():
            get_stock_list = getattr(position, "get_stock_list", None)
            if not callable(get_stock_list):
                raise RuntimeError("Qlib position does not expose get_stock_list")
            counts[pd.Timestamp(trade_date)] = len(get_stock_list())
        return pd.Series(counts, dtype="int64").sort_index()
    
    def _print_results(self, metrics: Dict, total_time: float):
        """Print result summary."""
        def _f(val, fmt='.6f'):
            return format(val, fmt) if isinstance(val, (int, float)) else 'N/A'

        print(f"\n{'='*50}")
        print("Backtest Results")
        print(f"{'='*50}")
        print("[IC Metrics]")
        print(f"  IC: {_f(metrics.get('IC'))}  ICIR: {_f(metrics.get('ICIR'))}")
        print(f"  Rank IC: {_f(metrics.get('Rank IC'))}  Rank ICIR: {_f(metrics.get('Rank ICIR'))}")
        print("[Strategy Metrics]")
        print(f"  Ann. Return: {_f(metrics.get('annualized_return'), '.4f')}  Max DD: {_f(metrics.get('max_drawdown'), '.4f')}")
        print(f"  Info Ratio: {_f(metrics.get('information_ratio'), '.4f')}  Calmar: {_f(metrics.get('calmar_ratio'), '.4f')}")
        print(f"Total time: {total_time:.1f}s")
        print(f"{'='*50}")
    
    def _save_results(self, metrics: Dict, exp_name: str, 
                     factor_source: str, num_factors: int, elapsed: float,
                     output_name: Optional[str] = None):
        """Save results."""
        output_dir = Path(self.config['experiment'].get('output_dir', './backtest_v2_results'))
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_name:
            output_file = f"{output_name}_backtest_metrics.json"
        else:
            output_file = self.config['experiment']['output_metrics_file']
        output_path = output_dir / output_file
        
        result_data = {
            "experiment_name": exp_name,
            "factor_source": factor_source,
            "num_factors": num_factors,
            "metrics": metrics,
            "config": {
                "data_range": f"{self.config['data']['start_time']} ~ {self.config['data']['end_time']}",
                "test_range": f"{self.config['dataset']['segments']['test'][0]} ~ {self.config['dataset']['segments']['test'][1]}",
                "backtest_range": f"{self.config['backtest']['backtest']['start_time']} ~ {self.config['backtest']['backtest']['end_time']}",
                "market": self.config['data']['market'],
                "benchmark": self.config['backtest']['backtest']['benchmark'],
                "effective_strategy": {
                    "class": self.config['backtest']['strategy']['class'],
                    "module_path": self.config['backtest']['strategy']['module_path'],
                    "kwargs": {
                        key: value
                        for key, value in self.config['backtest']['strategy'].get('kwargs', {}).items()
                        if key != 'signal'
                    },
                    "signal_order": "score_desc_instrument_asc",
                },
            },
            "elapsed_seconds": elapsed
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)
        
        print(f"Results saved: {output_path}")
        summary_file = output_dir / "batch_summary.json"
        summary_data = []
        if summary_file.exists():
            try:
                with open(summary_file, 'r', encoding='utf-8') as f:
                    summary_data = json.load(f)
            except:
                summary_data = []
        
        ann_ret = metrics.get('annualized_return')
        mdd = metrics.get('max_drawdown')
        calmar_ratio = None
        if ann_ret is not None and mdd is not None and mdd != 0:
            calmar_ratio = ann_ret / abs(mdd)
        
        summary_entry = {
            "name": output_name or exp_name,
            "num_factors": num_factors,
            "IC": metrics.get('IC'),
            "ICIR": metrics.get('ICIR'),
            "Rank_IC": metrics.get('Rank IC'),
            "Rank_ICIR": metrics.get('Rank ICIR'),
            "annualized_return": ann_ret,
            "information_ratio": metrics.get('information_ratio'),
            "max_drawdown": mdd,
            "calmar_ratio": calmar_ratio,
            "elapsed_seconds": elapsed
        }
        summary_data.append(summary_entry)
        
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary_data, f, ensure_ascii=False, indent=2)
        
        logger.debug(f"Appended to summary: {summary_file}")
