import json
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.research_shadow_supervisor_alert import (
    AlertConfig,
    append_event,
    build_event,
    emit_event,
    send_desktop_notification,
    send_webhook,
)


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self):
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse()


class ResearchShadowSupervisorAlertTests(unittest.TestCase):
    def _event(self):
        return build_event(
            "BACKEND_EXITED",
            supervisor_pid=101,
            child_pid=202,
            exit_code=7,
            child_runtime_seconds=9,
            restart_delay_seconds=5,
            occurred_at=datetime(2026, 8, 13, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    def test_event_log_is_private_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            event = self._event()
            append_event(path, event)
            append_event(path, event)

            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(rows, [event, event])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_subject_date_deduplicates_event_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            config = AlertConfig(
                event_log=path,
                desktop_notifications=True,
                webhook_url="https://alerts.example.invalid/hook",
                timeout_seconds=1.0,
            )
            event = build_event(
                "INTRADAY_TEMPORAL_DEGRADED",
                supervisor_pid=101,
                child_pid=202,
                subject_date="2026-08-14",
            )
            with (
                patch(
                    "scripts.research_shadow_supervisor_alert.send_desktop_notification"
                ) as desktop,
                patch(
                    "scripts.research_shadow_supervisor_alert.send_webhook"
                ) as webhook,
            ):
                self.assertEqual(emit_event(event, config), [])
                self.assertEqual(emit_event(event, config), [])

            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(rows, [event])
            self.assertEqual(event["subjectDate"], "2026-08-14")
            desktop.assert_called_once()
            webhook.assert_called_once()

    def test_subject_date_must_be_canonical_iso_date(self):
        with self.assertRaises(ValueError):
            build_event(
                "INTRADAY_TEMPORAL_DEGRADED",
                supervisor_pid=101,
                subject_date="20260814",
            )

    def test_webhook_payload_excludes_delivery_url(self):
        opener = FakeOpener()
        secret_url = "https://alerts.example.invalid/hook/secret-value"
        send_webhook(secret_url, self._event(), timeout_seconds=2.0, opener=opener)

        self.assertEqual(opener.request.full_url, secret_url)
        payload = json.loads(opener.request.data)
        self.assertEqual(payload["event"], "BACKEND_EXITED")
        self.assertNotIn("secret-value", opener.request.data.decode("utf-8"))
        self.assertEqual(opener.timeout, 2.0)

    def test_plaintext_remote_webhook_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            send_webhook(
                "http://alerts.example.invalid/hook",
                self._event(),
                timeout_seconds=2.0,
                opener=FakeOpener(),
            )

    def test_delivery_errors_never_include_webhook_secret(self):
        secret_url = "https://alerts.example.invalid/hook/secret-value"
        with tempfile.TemporaryDirectory() as tmp:
            config = AlertConfig(
                event_log=Path(tmp) / "events.jsonl",
                desktop_notifications=False,
                webhook_url=secret_url,
                timeout_seconds=1.0,
            )
            with patch(
                "scripts.research_shadow_supervisor_alert.send_webhook",
                side_effect=RuntimeError(secret_url),
            ):
                errors = emit_event(self._event(), config)

        self.assertEqual(errors, ["webhook:RuntimeError"])
        self.assertNotIn("secret-value", " ".join(errors))

    def test_desktop_notification_uses_argument_list_without_shell(self):
        with patch(
            "scripts.research_shadow_supervisor_alert.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            send_desktop_notification(self._event(), timeout_seconds=2.0)

        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertIn("QuantaAlpha supervisor", command)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_sleep_guard_and_intraday_events_have_fixed_secret_free_payloads(self):
        severity_by_event = {
            "SLEEP_GUARD_FAILED": "critical",
            "SLEEP_GUARD_RECOVERED": "info",
            "OFFICIAL_PROVIDER_UPDATE_ERROR": "critical",
            "OFFICIAL_PROVIDER_UPDATE_RECOVERED": "info",
            "OFFICIAL_PROVIDER_OVERDUE": "critical",
            "OFFICIAL_PROVIDER_FRESHNESS_RECOVERED": "info",
            "RESEARCH_SHADOW_REVIEW_REQUIRED": "critical",
            "INTRADAY_CAPTURE_ERROR": "critical",
            "INTRADAY_CAPTURE_RECOVERED": "info",
            "INTRADAY_TEMPORAL_DEGRADED": "critical",
            "INTRADAY_SHADOW_ERROR": "critical",
            "INTRADAY_SHADOW_RECOVERED": "info",
            "INTRADAY_SIGNAL_FROZEN": "info",
            "INTRADAY_DECISION_INTENT_FROZEN": "info",
            "INTRADAY_EXECUTION_PRICE_BOUND": "info",
            "INTRADAY_SESSION_FINALIZED": "info",
            "INTRADAY_SESSION_MISSED": "critical",
            "INTRADAY_RISK_STOPPED": "critical",
            "INTRADAY_PREFLIGHT_FAILED": "critical",
            "INTRADAY_PREFLIGHT_RECOVERED": "info",
        }
        expected_fields = {
            "schemaVersion",
            "kind",
            "event",
            "severity",
            "message",
            "occurredAt",
            "supervisorPid",
            "childPid",
            "exitCode",
            "childRuntimeSeconds",
            "restartDelaySeconds",
        }
        for event_name, severity in severity_by_event.items():
            with self.subTest(event=event_name):
                event = build_event(
                    event_name,
                    supervisor_pid=101,
                    child_pid=202,
                    occurred_at=datetime(
                        2026,
                        8,
                        13,
                        12,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                )
                rendered = json.dumps(event, sort_keys=True)
                self.assertEqual(set(event), expected_fields)
                self.assertEqual(event["severity"], severity)
                self.assertNotIn("TOKEN", rendered)
                self.assertNotIn("API_KEY", rendered)
                self.assertNotIn("secret", rendered.lower())


if __name__ == "__main__":
    unittest.main()
