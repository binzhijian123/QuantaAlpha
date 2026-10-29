import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from quantaalpha.utils.provider_lineage import (
    DEFAULT_FIELDS,
    validate_lineage_chain,
    verify_append_only_provider,
    write_lineage_proof,
)


def _write_feature(provider: Path, code: str, field: str, values: list[float]) -> None:
    path = provider / "features" / code.lower() / f"{field}.day.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.asarray([0.0, *values], dtype="<f4").tofile(path)


def _write_provider(
    root: Path,
    name: str,
    dates: list[str],
    stock_close: list[float],
) -> Path:
    provider = root / name
    calendar = provider / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("\n".join(dates) + "\n", encoding="utf-8")
    instruments = provider / "instruments" / "csi300.txt"
    instruments.parent.mkdir(parents=True)
    instruments.write_text(
        f"SH600000\t{dates[0]}\t{dates[-1]}\n",
        encoding="utf-8",
    )
    for field in DEFAULT_FIELDS:
        stock_values = stock_close if field == "close" else [1.0] * len(dates)
        benchmark_values = [3000.0] * len(dates)
        _write_feature(provider, "sh600000", field, stock_values)
        _write_feature(provider, "sh000300", field, benchmark_values)
    return provider


def _bind(proof: dict, root_fingerprint: str) -> dict:
    return {
        **proof,
        "runId": "shadow-v1",
        "declarationSha256": "d" * 64,
        "rootFingerprint": root_fingerprint,
        "baseFingerprint": root_fingerprint,
    }


class ProviderLineageTests(unittest.TestCase):
    def test_append_only_successor_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = _write_provider(
                root,
                "parent",
                ["2026-08-10", "2026-08-11"],
                [10.0, 11.0],
            )
            successor = _write_provider(
                root,
                "successor",
                ["2026-08-10", "2026-08-11", "2026-08-12"],
                [10.0, 11.0, 12.0],
            )

            proof = verify_append_only_provider(parent, successor)

            self.assertEqual(proof["status"], "PASS")
            self.assertTrue(proof["historyUnchanged"])
            self.assertEqual(proof["historyCutoff"], "2026-08-11")
            self.assertEqual(proof["firstAppendedDate"], "2026-08-12")
            self.assertEqual(proof["checkedFeatureFiles"], 14)

    def test_multi_session_successor_preserves_the_same_append_only_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = _write_provider(
                root,
                "parent",
                ["2026-08-10", "2026-08-11"],
                [10.0, 11.0],
            )
            successor = _write_provider(
                root,
                "successor",
                ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
                [10.0, 11.0, 12.0, 13.0],
            )

            proof = verify_append_only_provider(parent, successor)

            self.assertEqual(proof["status"], "PASS")
            self.assertEqual(proof["appendedSessions"], 2)
            self.assertEqual(proof["firstAppendedDate"], "2026-08-12")
            self.assertEqual(proof["successorLatestDate"], "2026-08-13")
            self.assertTrue(proof["calendarPrefixUnchanged"])
            self.assertTrue(proof["membershipHistoryUnchanged"])
            self.assertTrue(proof["historyUnchanged"])

    def test_historical_feature_rewrite_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = _write_provider(
                root,
                "parent",
                ["2026-08-10", "2026-08-11"],
                [10.0, 11.0],
            )
            successor = _write_provider(
                root,
                "successor",
                ["2026-08-10", "2026-08-11", "2026-08-12"],
                [10.5, 11.0, 12.0],
            )

            with self.assertRaisesRegex(ValueError, "feature history changed"):
                verify_append_only_provider(parent, successor)

    def test_multi_hop_chain_and_sidecars_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider0 = _write_provider(
                root,
                "provider0",
                ["2026-08-10", "2026-08-11"],
                [10.0, 11.0],
            )
            provider1 = _write_provider(
                root,
                "provider1",
                ["2026-08-10", "2026-08-11", "2026-08-12"],
                [10.0, 11.0, 12.0],
            )
            provider2 = _write_provider(
                root,
                "provider2",
                ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
                [10.0, 11.0, 12.0, 13.0],
            )
            proof0 = verify_append_only_provider(provider0, provider1)
            root_fingerprint = proof0["parentFingerprint"]
            proof0 = _bind(proof0, root_fingerprint)
            proof1 = _bind(
                verify_append_only_provider(provider1, provider2),
                root_fingerprint,
            )
            proof_dir = root / "proofs"
            first_path = proof_dir / f"{proof0['successorFingerprint']}.json"
            second_path = proof_dir / f"{proof1['successorFingerprint']}.json"
            write_lineage_proof(first_path, proof0)
            write_lineage_proof(second_path, proof1)

            chain = validate_lineage_chain(
                proof_dir,
                root_fingerprint=root_fingerprint,
                successor_fingerprint=proof1["successorFingerprint"],
                run_id="shadow-v1",
                declaration_sha256="d" * 64,
                successor_provider_path=provider2,
                verify_active_feature_tree=True,
            )
            self.assertEqual(len(chain), 2)

            _write_feature(provider2, "sh600000", "close", [10.0, 11.0, 12.0, 99.0])
            with self.assertRaisesRegex(ValueError, "feature tree differs"):
                validate_lineage_chain(
                    proof_dir,
                    root_fingerprint=root_fingerprint,
                    successor_fingerprint=proof1["successorFingerprint"],
                    run_id="shadow-v1",
                    declaration_sha256="d" * 64,
                    successor_provider_path=provider2,
                    verify_active_feature_tree=True,
                )

            second_path.write_text(
                json.dumps({**proof1, "historyUnchanged": False}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "sidecar mismatch"):
                validate_lineage_chain(
                    proof_dir,
                    root_fingerprint=root_fingerprint,
                    successor_fingerprint=proof1["successorFingerprint"],
                    run_id="shadow-v1",
                    declaration_sha256="d" * 64,
                )


if __name__ == "__main__":
    unittest.main()
