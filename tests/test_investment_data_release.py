import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from quantaalpha.utils.investment_data_release import (
    install_release_provider,
    load_release_manifest,
    query_official_release_manifest,
    query_official_release_tag,
    switch_runtime_provider,
    verify_release_assets,
)


def _manifest(
    archive: Path,
    tag: str = "2026-08-10",
    target_date: str | None = None,
) -> dict:
    return {
        "release_tag": tag,
        "target_trade_date": target_date or tag,
        "future_start_date": "2026-08-11",
        "future_end_date": "2026-12-31",
        "archive_size_bytes": archive.stat().st_size,
        "archive_sha256": f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}",
    }


def _write_release(root: Path, unsafe_name: str | None = None) -> Path:
    source = root / "release"
    source.mkdir()
    archive_path = source / "qlib_bin.tar.gz"
    files = {
        "qlib_bin/calendars/day.txt": b"2026-08-07\n2026-08-10\n",
        "qlib_bin/instruments/all.txt": b"SH600000\t2026-08-07\t2026-08-10\n",
        "qlib_bin/instruments/csi300.txt": b"".join(
            f"SH{600000 + index}\t2026-08-07\t2026-08-10\n".encode()
            for index in range(300)
        ),
        "qlib_bin/features/sh600000/close.day.bin": b"test",
    }
    if unsafe_name:
        files = {unsafe_name: b"unsafe"}
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    (source / "qlib_bin.manifest.json").write_text(
        json.dumps(_manifest(archive_path)), encoding="utf-8"
    )
    return source


class InvestmentDataReleaseTest(unittest.TestCase):
    def test_metadata_query_retries_transient_transport_failure(self):
        response = io.BytesIO(json.dumps({"tag_name": "2026-08-11"}).encode())
        with (
            patch(
                "quantaalpha.utils.investment_data_release.urllib.request.urlopen",
                side_effect=[TimeoutError("TLS handshake timed out"), response],
            ) as urlopen,
            patch(
                "quantaalpha.utils.investment_data_release.time.sleep"
            ) as sleep,
        ):
            result = query_official_release_tag()

        self.assertEqual(result, "2026-08-11")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_metadata_query_does_not_retry_permanent_client_error(self):
        error = urllib.error.HTTPError(
            "https://example.test/releases/latest",
            404,
            "Not Found",
            {},
            None,
        )
        with (
            patch(
                "quantaalpha.utils.investment_data_release.urllib.request.urlopen",
                side_effect=error,
            ) as urlopen,
            patch(
                "quantaalpha.utils.investment_data_release.time.sleep"
            ) as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "after 1 attempt"):
                query_official_release_tag()

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_lightweight_release_tag_query_validates_iso_date(self):
        with patch(
            "quantaalpha.utils.investment_data_release._request_json",
            return_value={"tag_name": "2026-08-11"},
        ) as request:
            self.assertEqual(query_official_release_tag(), "2026-08-11")
        request.assert_called_once()

        with patch(
            "quantaalpha.utils.investment_data_release._request_json",
            return_value={"tag_name": "latest"},
        ):
            with self.assertRaisesRegex(RuntimeError, "not an ISO date"):
                query_official_release_tag()

        with patch(
            "quantaalpha.utils.investment_data_release._request_json",
            return_value={"tag_name": "2026-99-99"},
        ):
            with self.assertRaisesRegex(RuntimeError, "not a valid date"):
                query_official_release_tag()

    def test_lightweight_manifest_query_accepts_weekend_release_tag(self):
        manifest = {
            "release_tag": "2026-08-09",
            "target_trade_date": "2026-08-07",
            "archive_size_bytes": 560_000_000,
            "archive_sha256": f"sha256:{'a' * 64}",
        }
        metadata = {
            "tag_name": "2026-08-09",
            "assets": [
                {
                    "name": "qlib_bin.manifest.json",
                    "browser_download_url": "https://example.test/manifest.json",
                }
            ],
        }
        with patch(
            "quantaalpha.utils.investment_data_release._request_json",
            side_effect=[metadata, manifest],
        ) as request:
            result = query_official_release_manifest()

        self.assertEqual(result["release_tag"], "2026-08-09")
        self.assertEqual(result["target_trade_date"], "2026-08-07")
        self.assertEqual(request.call_count, 2)

    def test_manifest_rejects_target_date_after_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "release_tag": "2026-08-09",
                        "target_trade_date": "2026-08-10",
                        "archive_size_bytes": 1,
                        "archive_sha256": f"sha256:{'a' * 64}",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError, "cannot be later than release_tag"
            ):
                load_release_manifest(path)

    def test_installs_and_reuses_verified_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _write_release(root)
            output = root / "provider"
            installed = install_release_provider(source, output)
            self.assertEqual(installed["status"], "INSTALLED")
            self.assertEqual(installed["provider"]["calendarEnd"], "2026-08-10")
            self.assertEqual(installed["provider"]["activeCsi300"], 300)
            reused = install_release_provider(source, output)
            self.assertEqual(reused["status"], "REUSED")
            self.assertEqual(
                reused["provider"]["providerFingerprint"],
                installed["provider"]["providerFingerprint"],
            )
            (output / "features" / "sh600000" / "close.day.bin").write_bytes(
                b"edit"
            )
            with self.assertRaisesRegex(RuntimeError, "content differs"):
                install_release_provider(source, output)

    def test_rejects_archive_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _write_release(Path(temporary))
            with (source / "qlib_bin.tar.gz").open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                verify_release_assets(source)

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _write_release(root, unsafe_name="../../escaped")
            with self.assertRaisesRegex(RuntimeError, "unsafe release archive path"):
                install_release_provider(source, root / "provider")
            self.assertFalse((root / "escaped").exists())

    def test_switch_is_explicit_and_refuses_real_qlib_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = root / "provider"
            provider.mkdir()
            dotenv = root / ".env"
            dotenv.write_text("KEEP=value\nQLIB_DATA_DIR=old\n", encoding="utf-8")
            dotenv.chmod(0o644)
            qlib_link = root / ".qlib" / "cn_data"
            previous_umask = os.umask(0o077)
            try:
                result = switch_runtime_provider(provider, dotenv, qlib_link)
            finally:
                os.umask(previous_umask)
            self.assertEqual(Path(result["providerPath"]), provider.resolve())
            self.assertIn("KEEP=value", dotenv.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(dotenv.stat().st_mode), 0o600)
            self.assertFalse(
                dotenv.with_name(f".{dotenv.name}.{os.getpid()}.tmp").exists()
            )
            self.assertEqual(qlib_link.resolve(), provider.resolve())
            qlib_link.unlink()
            qlib_link.mkdir()
            with self.assertRaisesRegex(RuntimeError, "real Qlib directory"):
                switch_runtime_provider(provider, dotenv, qlib_link)


if __name__ == "__main__":
    unittest.main()
