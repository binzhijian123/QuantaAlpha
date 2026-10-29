#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify that a Qlib provider can be read by qlib.data.D.")
    parser.add_argument("provider_uri")
    parser.add_argument("--market", default="csi300")
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    args = parser.parse_args()

    import qlib
    from qlib.data import D

    provider = Path(args.provider_uri).expanduser().resolve()
    qlib.init(provider_uri=str(provider), region="cn")
    instruments = D.instruments(args.market)
    fields = ["$open", "$close", "$high", "$low", "$volume", "$amount"]
    df = D.features(
        instruments,
        fields,
        start_time=args.start_time,
        end_time=args.end_time,
        freq="day",
    )
    payload = {
        "providerUri": str(provider),
        "market": args.market,
        "shape": list(df.shape),
        "indexNames": list(df.index.names),
        "columns": list(df.columns),
        "nonNullOpen": int(df["$open"].notna().sum()) if "$open" in df else 0,
        "start": str(df.index.get_level_values("datetime").min()) if not df.empty else None,
        "end": str(df.index.get_level_values("datetime").max()) if not df.empty else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["nonNullOpen"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
