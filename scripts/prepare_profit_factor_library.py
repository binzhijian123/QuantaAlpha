#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantaalpha.backtest.custom_factor_calculator import CustomFactorCalculator
from quantaalpha.factors.regulator.consistency_checker import FactorConsistencyChecker
from quantaalpha.utils.qlib_provider import provider_contract_fingerprint


DEFAULT_RAW_FIELDS = {"open", "high", "low", "close", "volume", "vwap"}


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _factor_hypothesis(info: dict[str, Any]) -> str:
    return str(
        info.get("hypothesis")
        or (info.get("metadata") or {}).get("hypothesis")
        or ""
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _factor_code_contract() -> dict[str, str]:
    paths = {
        "calculator_code_sha256": PROJECT_ROOT
        / "quantaalpha"
        / "backtest"
        / "custom_factor_calculator.py",
        "parser_code_sha256": PROJECT_ROOT
        / "quantaalpha"
        / "factors"
        / "coder"
        / "expr_parser.py",
        "function_library_sha256": PROJECT_ROOT
        / "quantaalpha"
        / "factors"
        / "coder"
        / "function_lib.py",
        "preparation_code_sha256": PROJECT_ROOT
        / "scripts"
        / "prepare_profit_factor_library.py",
    }
    return {field: _sha256(path) for field, path in paths.items()}


def _additional_raw_fields(factors: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for info in factors:
        expression = str(info.get("factor_expression") or "")
        for field in re.findall(r"\$([A-Za-z][A-Za-z0-9_]*)", expression):
            if field not in DEFAULT_RAW_FIELDS and field not in fields:
                fields.append(field)
    return fields


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recheck and recompute a frozen factor pool on one Qlib provider."
    )
    parser.add_argument("--source", required=True, help="Source factor-library JSON.")
    parser.add_argument("--provider", required=True, help="Versioned Qlib provider.")
    parser.add_argument("--output", required=True, help="New immutable factor-library JSON.")
    parser.add_argument("--cache-root", default=None, help="Fresh cache output directory.")
    parser.add_argument(
        "--reuse-library",
        default=None,
        help="Immutable prepared library whose validated matching caches may be reused.",
    )
    parser.add_argument("--market", default="csi300")
    parser.add_argument("--benchmark-instrument", default="SH000300")
    parser.add_argument("--start", default="2014-01-01")
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--static-instrument-history",
        action="store_true",
        help=(
            "Load full price histories for the union of market members observed "
            "inside the frozen membership window."
        ),
    )
    parser.add_argument(
        "--membership-start",
        default=None,
        help="Start of the market-membership discovery window.",
    )
    parser.add_argument(
        "--membership-end",
        default=None,
        help="End of the market-membership discovery window.",
    )
    parser.add_argument(
        "--consistency-evidence",
        default=None,
        help="Frozen independent-review JSON; otherwise use the configured LLM checker.",
    )
    parser.add_argument(
        "--trial-universe",
        action="store_true",
        help=(
            "Prepare every frozen source factor for homogeneous strategy trials. "
            "Reuses persisted strict consistency evidence and does not call an LLM."
        ),
    )
    args = parser.parse_args()

    source = _resolve(args.source)
    provider = _resolve(args.provider)
    output = _resolve(args.output)
    cache_root = (
        _resolve(args.cache_root)
        if args.cache_root
        else PROJECT_ROOT / "data" / "results" / "factor_cache" / output.stem
    )
    report_path = output.with_suffix(".audit.json")
    for target in (output, report_path):
        if target.exists():
            raise FileExistsError(f"refusing to overwrite immutable artifact: {target}")

    source_payload = json.loads(source.read_text(encoding="utf-8"))
    source_factors = source_payload.get("factors") or {}
    contract = provider_contract_fingerprint(provider, args.market)
    provider_fingerprint = contract["fingerprint"]
    factor_code_contract = _factor_code_contract()
    membership_start = args.membership_start or args.start
    membership_end = args.membership_end or args.end
    if args.static_instrument_history and (
        pd.Timestamp(membership_end) < pd.Timestamp(membership_start)
    ):
        raise ValueError("membership end must be on or after membership start")
    instrument_scope = {
        "mode": (
            "static_union_of_historical_market_members"
            if args.static_instrument_history
            else "point_in_time_market_membership"
        ),
        "market": args.market,
        "membership_start": membership_start if args.static_instrument_history else None,
        "membership_end": membership_end if args.static_instrument_history else None,
    }
    reuse_path = _resolve(args.reuse_library) if args.reuse_library else None
    reuse_payload = (
        json.loads(reuse_path.read_text(encoding="utf-8")) if reuse_path else {}
    )
    reuse_metadata = reuse_payload.get("metadata") or {}
    reuse_factors = reuse_payload.get("factors") or {}
    if reuse_path:
        if (reuse_metadata.get("provider_contract") or {}).get("fingerprint") != provider_fingerprint:
            raise RuntimeError("reuse library provider fingerprint does not match")
        if reuse_metadata.get("calculation_range") != [args.start, args.end]:
            raise RuntimeError("reuse library calculation range does not match")
        if reuse_metadata.get("instrument_scope") != instrument_scope:
            raise RuntimeError("reuse library instrument scope does not match")
    evidence_payload = None
    evidence_checks: dict[str, dict[str, Any]] = {}
    evidence_path = _resolve(args.consistency_evidence) if args.consistency_evidence else None
    if evidence_path:
        evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence_payload.get("source_library_sha256") != _sha256(source):
            raise RuntimeError("semantic evidence source-library hash does not match")
        operator_path = _resolve(
            str(
                evidence_payload.get("operator_library")
                or "quantaalpha/factors/coder/function_lib.py"
            )
        )
        if evidence_payload.get("operator_library_sha256") != _sha256(operator_path):
            raise RuntimeError("semantic evidence operator-library hash does not match")
        evidence_checks = evidence_payload.get("checks") or {}
    checker = None
    if not evidence_path and not args.trial_universe:
        checker = FactorConsistencyChecker(enabled=True, strict_mode=True)
    checked_at = datetime.now(timezone.utc).isoformat()

    prepared: dict[str, dict[str, Any]] = {}
    checks: dict[str, dict[str, Any]] = {}
    passing_for_compute: list[dict[str, Any]] = []
    id_by_name: dict[str, str] = {}
    feedback_accepted_count = 0
    failed_consistency_ids: list[str] = []
    reused_ids: list[str] = []

    for factor_id, original in source_factors.items():
        feedback_accepted = (original.get("feedback") or {}).get("decision") is True
        if feedback_accepted:
            feedback_accepted_count += 1
        if not args.trial_universe and not feedback_accepted:
            continue
        info = copy.deepcopy(original)
        if evidence_path:
            evidence = copy.deepcopy(evidence_checks.get(factor_id) or {})
            if "is_consistent" not in evidence:
                raise RuntimeError(
                    f"semantic evidence is missing prepared factor: {factor_id}"
                )
            checker_name = str(evidence_payload.get("reviewer") or evidence_path)
            is_consistent = evidence.get("is_consistent") is True
        elif args.trial_universe:
            source_gate = info.get("quality_gate") or {}
            evidence = copy.deepcopy(source_gate.get("consistency_check") or {})
            is_consistent = (
                source_gate.get("consistency_enabled") is True
                and source_gate.get("consistency_passed") is True
                and evidence.get("is_consistent") is True
            )
            checker_name = "frozen_source_library_quality_gate"
        else:
            result = checker.check_consistency(
                hypothesis=_factor_hypothesis(info),
                factor_name=str(info.get("factor_name") or factor_id),
                factor_description=str(info.get("factor_description") or ""),
                factor_formulation=str(info.get("factor_formulation") or ""),
                factor_expression=str(info.get("factor_expression") or ""),
            )
            evidence = result.to_dict()
            checker_name = "FactorConsistencyChecker.strict"
            is_consistent = evidence.get("is_consistent") is True
        info["quality_gate"] = {
            **(info.get("quality_gate") or {}),
            "consistency_enabled": True,
            "consistency_passed": is_consistent,
            "consistency_check": evidence,
            "checked_at": checked_at,
            "checker": checker_name,
            "evidence_path": str(evidence_path) if evidence_path else None,
        }
        info["cache_location"] = {}
        prepared[factor_id] = info
        checks[factor_id] = evidence
        if not is_consistent:
            failed_consistency_ids.append(factor_id)
        if is_consistent:
            factor_name = str(info.get("factor_name") or factor_id)
            if factor_name in id_by_name:
                if not args.trial_universe:
                    raise RuntimeError(
                        "duplicate accepted factor name: "
                        f"{factor_name} ({id_by_name[factor_name]}, {factor_id})"
                    )
                info.setdefault("metadata", {})["original_factor_name"] = factor_name
                factor_name = f"{factor_name}__{factor_id[:8]}"
                info["factor_name"] = factor_name
            id_by_name[factor_name] = factor_id
            reusable = reuse_factors.get(factor_id) or {}
            reusable_location = reusable.get("cache_location") or {}
            reusable_path = Path(str(reusable_location.get("result_h5_path") or ""))
            expression = str(info.get("factor_expression") or "")
            expected_expression_hash = hashlib.sha256(
                expression.encode("utf-8")
            ).hexdigest()
            can_reuse = bool(reusable_location) and all(
                [
                    str(reusable.get("factor_name") or factor_id) == factor_name,
                    str(reusable.get("factor_expression") or "") == expression,
                    reusable_location.get("provider_fingerprint") == provider_fingerprint,
                    reusable_location.get("market") == args.market,
                    reusable_location.get("date_range") == [args.start, args.end],
                    reusable_location.get("expression_sha256") == expected_expression_hash,
                    reusable_path.is_file(),
                ]
            )
            if (
                can_reuse
                and (info.get("metadata") or {}).get(
                    "cache_code_binding_required"
                )
                is True
            ):
                can_reuse = all(
                    reusable_location.get(field) == digest
                    for field, digest in factor_code_contract.items()
                )
            if can_reuse:
                expected_cache_hash = str(
                    reusable_location.get("result_h5_sha256") or ""
                )
                can_reuse = bool(expected_cache_hash) and _sha256(
                    reusable_path
                ) == expected_cache_hash
            if can_reuse:
                info["cache_location"] = copy.deepcopy(reusable_location)
                reused_ids.append(factor_id)
            else:
                passing_for_compute.append(info)

    if not prepared:
        raise RuntimeError("source library has no factors eligible for preparation")
    if args.trial_universe and failed_consistency_ids:
        _write_json(
            report_path,
            {
                "status": "FAIL",
                "reason": "Trial universe contains factors without valid consistency evidence.",
                "failedConsistencyFactorIds": failed_consistency_ids,
                "checks": checks,
                "providerContract": contract,
            },
        )
        raise RuntimeError(
            f"trial-universe consistency evidence incomplete: {failed_consistency_ids}"
        )
    if not passing_for_compute and not reused_ids:
        _write_json(
            report_path,
            {
                "status": "FAIL",
                "reason": "No factor passed the strict semantic consistency check.",
                "checks": checks,
                "providerContract": contract,
            },
        )
        raise RuntimeError("no factor passed strict semantic consistency")

    additional_fields = _additional_raw_fields(list(prepared.values()))
    compute_additional_fields = _additional_raw_fields(passing_for_compute)
    calculation_config = {
        "data": {
            "provider_uri": str(provider),
            "provider_strict": True,
            "region": "cn",
            "market": args.market,
            "start_time": args.start,
            "end_time": args.end,
            "additional_fields": compute_additional_fields,
            "benchmark_instrument": args.benchmark_instrument,
            "static_instrument_history": args.static_instrument_history,
            "instrument_membership_start_time": membership_start,
            "instrument_membership_end_time": membership_end,
        }
    }
    computed = pd.DataFrame()
    if passing_for_compute:
        calculator = CustomFactorCalculator(
            data_df=None,
            cache_dir=cache_root,
            auto_extract_cache=False,
            config=calculation_config,
            cache_namespace=provider_fingerprint,
        )
        computed = calculator.calculate_factors_batch(
            passing_for_compute,
            use_cache=False,
            skip_compute=False,
        )
        if computed.empty:
            raise RuntimeError("factor recomputation returned no values")

    cache_root.mkdir(parents=True, exist_ok=True)
    cached_ids: list[str] = list(reused_ids)
    for factor_name in computed.columns:
        factor_id = id_by_name.get(str(factor_name))
        if factor_id is None:
            continue
        factor_dir = cache_root / factor_id
        factor_dir.mkdir(parents=True, exist_ok=False)
        cache_path = factor_dir / "result.h5"
        computed[factor_name].rename("factor").to_hdf(
            cache_path,
            key="data",
            mode="w",
        )
        expression = str(prepared[factor_id].get("factor_expression") or "")
        prepared[factor_id]["cache_location"] = {
            "result_h5_path": str(cache_path),
            "result_h5_sha256": _sha256(cache_path),
            "provider_path": str(provider),
            "provider_fingerprint": provider_fingerprint,
            "market": args.market,
            "date_range": [args.start, args.end],
            "expression_sha256": hashlib.sha256(expression.encode("utf-8")).hexdigest(),
            "calculator": "CustomFactorCalculator.v2",
            **factor_code_contract,
        }
        cached_ids.append(factor_id)

    missing = sorted(set(id_by_name.values()) - set(cached_ids))
    status = "PASS" if not missing else "FAIL"
    payload = {
        "metadata": {
            **(source_payload.get("metadata") or {}),
            "created_at": checked_at,
            "last_updated": checked_at,
            "version": (
                "profit-trial-provider-v1"
                if args.trial_universe
                else "profit-provider-v1"
            ),
            "preparation_mode": (
                "trial_universe" if args.trial_universe else "accepted_only"
            ),
            "source_library": str(source),
            "source_library_sha256": _sha256(source),
            "source_factor_count": len(source_factors),
            "upstream_source_library": (source_payload.get("metadata") or {}).get(
                "source_library"
            ),
            "upstream_source_library_sha256": (
                source_payload.get("metadata") or {}
            ).get("source_library_sha256"),
            "trial_universe_library_sha256": (
                _sha256(source)
                if args.trial_universe
                else (source_payload.get("metadata") or {}).get(
                    "source_library_sha256"
                )
            ),
            "provider_contract": contract,
            "factor_code_contract": factor_code_contract,
            "calculation_range": [args.start, args.end],
            "instrument_scope": instrument_scope,
            "additional_raw_fields": additional_fields,
            "semantic_evidence": (
                "frozen_source_library_quality_gate"
                if args.trial_universe
                else str(evidence_path) if evidence_path else "configured_llm"
            ),
            "semantic_evidence_sha256": _sha256(evidence_path)
            if evidence_path
            else None,
            "reuse_library": str(reuse_path) if reuse_path else None,
            "reuse_library_sha256": _sha256(reuse_path) if reuse_path else None,
            "reused_factor_count": len(reused_ids),
            "feedback_accepted_count": feedback_accepted_count,
            "prepared_factor_count": len(prepared),
            "cached_factor_count": len(cached_ids),
            "total_factors": len(prepared),
        },
        "factors": prepared,
    }
    _write_json(output, payload)
    _write_json(
        report_path,
        {
            "status": status,
            "sourceLibrary": str(source),
            "sourceLibrarySha256": _sha256(source),
            "sourceFactorCount": len(source_factors),
            "preparationMode": (
                "trial_universe" if args.trial_universe else "accepted_only"
            ),
            "feedbackAcceptedCount": feedback_accepted_count,
            "preparedFactorCount": len(prepared),
            "outputLibrary": str(output),
            "providerContract": contract,
            "factorCodeContract": factor_code_contract,
            "instrumentScope": instrument_scope,
            "additionalRawFields": additional_fields,
            "semanticEvidence": (
                str(evidence_path)
                if evidence_path
                else "frozen_source_library_quality_gate"
                if args.trial_universe
                else "configured_llm"
            ),
            "reuseLibrary": str(reuse_path) if reuse_path else None,
            "reusedFactorIds": reused_ids,
            "consistencyChecks": checks,
            "cachedFactorIds": cached_ids,
            "missingFactorIds": missing,
        },
    )
    if missing:
        raise RuntimeError(f"factor cache generation incomplete: {missing}")

    print(
        json.dumps(
            {
                "status": status,
                "output": str(output),
                "cacheRoot": str(cache_root),
                "factorCount": len(cached_ids),
                "providerFingerprint": provider_fingerprint,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
