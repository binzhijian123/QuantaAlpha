from __future__ import annotations

import hashlib
import json
import pickle
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from quantaalpha.trading.market_data import QlibBinaryDailySource
from quantaalpha.trading.research_shadow_rehearsal import (
    _state_summary,
    append_synthetic_session,
    clone_provider_copy_on_write,
    copy_snapshot_for_rehearsal,
    snapshot_model_contract,
)
from quantaalpha.utils.provider_lineage import (
    DEFAULT_FIELDS,
    verify_append_only_provider,
)


class _DummyBooster:
    def model_to_string(self):
        return "fixed-lightgbm-model"


class _DummyModel:
    def __init__(self):
        self.model = _DummyBooster()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_feature(
    provider: Path,
    code: str,
    field: str,
    values: list[float],
) -> None:
    path = provider / "features" / code.lower() / f"{field}.day.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.asarray([0.0, *values], dtype="<f4").tofile(path)


def _write_provider(root: Path) -> Path:
    provider = root / "provider"
    calendar = provider / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("2026-08-10\n2026-08-11\n", encoding="utf-8")
    instruments = provider / "instruments" / "csi300.txt"
    instruments.parent.mkdir(parents=True)
    instruments.write_text(
        "SH600000\t2026-08-10\t2026-08-11\n",
        encoding="utf-8",
    )
    for code, base in (("sh600000", 10.0), ("sh000300", 4000.0)):
        for field in DEFAULT_FIELDS:
            if field == "volume":
                values = [1_000_000.0, 1_100_000.0]
            elif field == "factor":
                values = [1.0, 1.0]
            else:
                values = [base, base * 1.01]
            _write_feature(provider, code, field, values)
    (provider / "qlib_bin.manifest.json").write_text(
        json.dumps(
            {
                "release_tag": "2026-08-11",
                "archive_sha256": "sha256:" + "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    return provider


class ResearchShadowRehearsalTests(unittest.TestCase):
    def test_synthetic_state_summary_is_never_evidence_eligible(self):
        arm = {
            "account": {"lastTradeDate": "2026-08-12"},
            "positions": [{}],
            "pendingSignal": None,
            "equityCurve": [{}],
        }
        summary = _state_summary(
            {
                "pairedEvidence": {"evidenceEligible": True},
                "arms": {"C1": arm, "B0": arm},
            }
        )

        self.assertFalse(summary["pairedEvidence"]["evidenceEligible"])
        self.assertTrue(summary["pairedEvidence"]["engineStateEvidenceEligible"])
        self.assertEqual(
            summary["pairedEvidence"]["evidenceExclusionReason"],
            "synthetic_rehearsal_market_data",
        )

    def test_synthetic_session_is_append_only_and_keeps_parent_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = _write_provider(root)
            successor = root / "successor"
            parent_hashes = {
                path.relative_to(parent).as_posix(): _sha256(path)
                for path in parent.rglob("*")
                if path.is_file()
            }
            shutil.copytree(parent, successor)

            result = append_synthetic_session(successor, "2026-08-12")
            proof = verify_append_only_provider(parent, successor)
            source = QlibBinaryDailySource(successor)
            bars = source.get_bars(["600000.SH", "000300.SH"], "2026-08-12")

            self.assertEqual(result["parentDate"], "2026-08-11")
            self.assertEqual(result["tradeDate"], "2026-08-12")
            self.assertEqual(result["activeSymbols"], 1)
            self.assertEqual(proof["appendedSessions"], 1)
            self.assertTrue(proof["historyUnchanged"])
            self.assertEqual(source.latest_dates(1), ["2026-08-12"])
            self.assertEqual(sorted(bars), ["000300.SH", "600000.SH"])
            self.assertGreater(bars["600000.SH"].open, 0)
            self.assertGreater(bars["600000.SH"].volume, 0)
            self.assertEqual(
                parent_hashes,
                {
                    path.relative_to(parent).as_posix(): _sha256(path)
                    for path in parent.rglob("*")
                    if path.is_file()
                },
            )
            manifest = json.loads(
                (successor / "qlib_bin.manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["rehearsal_synthetic"]["synthetic"])
            self.assertFalse(
                manifest["rehearsal_synthetic"]["profitability_evidence"]
            )

    def test_synthetic_session_rejects_non_forward_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _write_provider(Path(tmp))
            with self.assertRaisesRegex(ValueError, "must follow"):
                append_synthetic_session(provider, "2026-08-11")

    def test_provider_clone_never_uses_hard_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _write_provider(root)
            destination = root / "clone"

            result = clone_provider_copy_on_write(
                source,
                destination,
                allow_full_copy=True,
            )
            source_calendar = source / "calendars" / "day.txt"
            cloned_calendar = destination / "calendars" / "day.txt"
            self.assertNotEqual(source_calendar.stat().st_ino, cloned_calendar.stat().st_ino)
            cloned_calendar.write_text("changed\n", encoding="utf-8")
            self.assertEqual(
                source_calendar.read_text(encoding="utf-8"),
                "2026-08-10\n2026-08-11\n",
            )
            self.assertIn(result["method"], {"apfs_clonefile", "full_copy"})

    def test_snapshot_copy_preserves_bytes_and_model_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pkl"
            destination = root / "copied.pkl"
            payload = {
                "version": 1,
                "model": _DummyModel(),
                "featureColumns": ["alpha", "beta"],
                "factors": [
                    {"name": "alpha", "expression": "$close"},
                    {"name": "beta", "expression": "$volume"},
                ],
                "executionContract": {"topK": 50, "nDrop": 1},
                "modelConfig": {"seed": 42},
                "declarationPath": "/production/declaration.json",
                "declarationSha256": "a" * 64,
                "providerLineageProofDir": "/production/proofs",
            }
            with source.open("wb") as stream:
                pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
            source_sha = _sha256(source)

            result = copy_snapshot_for_rehearsal(
                source,
                destination,
            )
            with destination.open("rb") as stream:
                copied = pickle.load(stream)

            self.assertEqual(_sha256(source), source_sha)
            self.assertEqual(_sha256(destination), source_sha)
            self.assertTrue(result["byteIdentical"])
            self.assertEqual(
                result["modelContract"], snapshot_model_contract(copied)
            )
            self.assertEqual(
                copied["declarationPath"], "/production/declaration.json"
            )
            self.assertEqual(
                copied["providerLineageProofDir"], "/production/proofs"
            )
            self.assertEqual(
                destination.with_suffix(".pkl.sha256").read_text(
                    encoding="ascii"
                ).strip(),
                _sha256(destination),
            )


if __name__ == "__main__":
    unittest.main()
