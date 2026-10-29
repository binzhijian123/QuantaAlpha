#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "frontend-v2" / "backend"
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_data.providers.tonghuashun_quantapi import (
    TonghuashunQuantApiClient,
    TonghuashunQuantApiError,
    summarize_quantapi_response,
)


load_dotenv(PROJECT_ROOT / ".env", override=False)


def _load_request(path: str) -> dict:
    request_path = Path(path).expanduser().resolve()
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request JSON must contain one object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify Tonghuashun QuantAPI authentication or inspect a generated "
            "basic/date-sequence response without printing source values."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="Exchange the configured refresh token for the current access token only.",
    )
    mode.add_argument(
        "--field-probe",
        action="store_true",
        help="Run one explicit source-only request and print schema/row counts only.",
    )
    parser.add_argument(
        "--service",
        choices=("basic_data_service", "date_sequence"),
    )
    parser.add_argument(
        "--request-json",
        help="Path to a Super Command generated formData JSON object without credentials.",
    )
    parser.add_argument(
        "--confirm-quota-use",
        action="store_true",
        help="Required for --field-probe because the request consumes account quota.",
    )
    args = parser.parse_args()

    if args.field_probe:
        if not args.service or not args.request_json:
            parser.error("--field-probe requires --service and --request-json")
        if not args.confirm_quota_use:
            parser.error("--field-probe requires --confirm-quota-use")
    elif args.service or args.request_json or args.confirm_quota_use:
        parser.error("field-probe arguments cannot be used with --preflight")

    try:
        client = TonghuashunQuantApiClient()
        if args.preflight:
            result = client.preflight()
        else:
            payload = _load_request(args.request_json)
            result = summarize_quantapi_response(client.request(args.service, payload))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TonghuashunQuantApiError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": str(exc),
                    "tokenPersisted": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
