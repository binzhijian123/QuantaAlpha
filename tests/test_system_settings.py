import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend-v2/backend"))
from system_settings import SettingsStore, install_settings_routes


class SystemSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = SettingsStore(self.root)

    def test_first_install_private_round_trip_and_secret_not_returned(self):
        key = "fixture-credential-123456789"
        self.store.save({"OPENAI_API_KEY": key, "CHAT_MODEL": "my-custom-model",
                         "OPENAI_BASE_URL": "https://example.test/v1"})
        self.assertEqual(self.store.read()["OPENAI_API_KEY"], key)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        public = self.store.public()
        self.assertTrue(public["apiKeyConfigured"])
        self.assertNotIn(key, json.dumps(public))
        self.assertNotIn("OPENAI_API_KEY", public["env"])

    def test_blank_key_retains_existing_and_unrelated_env_survives(self):
        self.store.path.write_text("# keep this\nPRIVATE_TOKEN=fixture-private\nOPENAI_API_KEY=original\n")
        self.store.save({"OPENAI_API_KEY": "", "CHAT_MODEL": "model-v2"})
        self.assertEqual(self.store.read()["OPENAI_API_KEY"], "original")
        self.assertEqual(self.store.read()["PRIVATE_TOKEN"], "fixture-private")
        self.assertIn("# keep this", self.store.path.read_text())
        self.assertNotIn("PRIVATE_TOKEN", self.store.public()["env"])

    def test_imported_url_credentials_are_not_disclosed(self):
        self.store.path.write_text('OPENAI_BASE_URL="https://name:fixture-pass@example.test/v1?token=fixture"\n')
        self.assertEqual(self.store.public()["env"]["OPENAI_BASE_URL"], "")

    def test_failed_atomic_replace_preserves_previous_config(self):
        self.store.save({"CHAT_MODEL": "original"})
        before = self.store.path.read_bytes()
        with patch("system_settings.os.replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.store.save({"CHAT_MODEL": "replacement"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.env.*')), [])

    def test_quoted_paths_round_trip_and_shell_consumer_compatibility(self):
        value = str(self.root / 'my data "sample"')
        self.store.save({"QLIB_DATA_DIR": value})
        self.assertEqual(self.store.read()["QLIB_DATA_DIR"], value)
        self.assertEqual(self.store.read()["QLIB_PROVIDER_URI"], value)
        if os.name != "nt":
            result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s" "$QLIB_DATA_DIR"',
                                     "test", str(self.store.path)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, value)

    def test_paths_are_absolute_for_all_consumers_including_legacy_values(self):
        self.store.save({"QLIB_DATA_DIR": "data/provider", "DATA_RESULTS_DIR": "~/qa-results"})
        self.assertEqual(self.store.read()["QLIB_DATA_DIR"], str(self.root / "data/provider"))
        self.assertEqual(self.store.read()["QLIB_PROVIDER_URI"], str(self.root / "data/provider"))
        self.assertEqual(self.store.read()["DATA_RESULTS_DIR"], str(Path.home() / "qa-results"))
        self.store.path.write_text("QLIB_DATA_DIR=legacy/provider\n")
        self.assertEqual(self.store.read()["QLIB_DATA_DIR"], str(self.root / "legacy/provider"))

    def test_rejects_injection_unknown_fields_and_malformed_urls_without_write(self):
        for payload in [{"OPENAI_API_KEY": "first\nOTHER=x"}, {"CHAT_MODEL": "$(touch nope)"},
                        {"OPENAI_API_KEY": "bad`command`"}, {"UNKNOWN": "value"},
                        {"CHAT_MODEL": 42}, {"OPENAI_BASE_URL": "https://user:pass@example.test"},
                        {"OPENAI_BASE_URL": "file:///tmp/local"}]:
            with self.subTest(fields=list(payload)):
                with self.assertRaises(ValueError):
                    self.store.save(payload)
        self.assertFalse(self.store.path.exists())

    def test_environment_checks_do_not_create_output_or_call_network(self):
        provider = self.root / "provider"
        for p in ["calendars", "features", "instruments"]:
            (provider / p).mkdir(parents=True)
        (provider / "calendars/day.txt").write_text("2026-10-22\n")
        output = self.root / "new/results"
        self.store.save({"QLIB_DATA_DIR": str(provider), "DATA_RESULTS_DIR": str(output)})
        checks = {c["name"]: c for c in self.store.environment()["checks"]}
        self.assertTrue(checks["Qlib 数据结构"]["passed"])
        self.assertTrue(checks["结果目录"]["passed"])
        self.assertFalse(checks["API 配置"]["passed"])
        self.assertFalse(output.exists())

    def test_api_replaces_routes_and_never_echoes_bad_credential(self):
        app = FastAPI()
        @app.get("/api/v1/system/config")
        async def old_get():
            return {"legacy": True}
        @app.put("/api/v1/system/config")
        async def old_put():
            return {"legacy": True}
        backend = SimpleNamespace(app=app, PROJECT_ROOT=self.root, _find_factor_jsons=lambda: [])
        install_settings_routes(backend)
        for method in ["GET", "PUT"]:
            routes = [r for r in app.routes if getattr(r, "path", "") == "/api/v1/system/config"
                      and method in r.methods]
            self.assertEqual(len(routes), 1)
        client = TestClient(app)
        secret = "fixture-credential\nBAD=payload"
        response = client.put("/api/v1/system/config", json={"OPENAI_API_KEY": secret})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("fixture-credential", response.text)
        self.assertEqual(client.get("/api/v1/system/config").json()["data"]["env"]["CHAT_MODEL"], "")
        response = client.put("/api/v1/system/config", json={"CHAT_MODEL": "user-model"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(backend._load_dotenv_dict()["CHAT_MODEL"], "user-model")


if __name__ == "__main__":
    unittest.main()
