import json
import os
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.manage_research_shadow_supervisor import (
    HEALTH_URL,
    LOG_PATH,
    PID_FILE,
    PROJECT_ROOT,
    SCREEN_NAME,
    SLEEP_GUARD_PID_FILE,
    WRAPPER_PATH,
    intraday_preflight_enabled,
    prevent_idle_sleep_enabled,
    screen_command,
    sleep_guard_pid,
    start,
    supervisor_pid,
)


class ResearchShadowSupervisorTests(unittest.TestCase):
    def test_screen_command_uses_repo_wrapper_and_log_without_secrets(self):
        with (
            patch(
                "scripts.manage_research_shadow_supervisor.prevent_idle_sleep_enabled",
                return_value=True,
            ),
            patch(
                "scripts.manage_research_shadow_supervisor.intraday_preflight_enabled",
                return_value=True,
            ),
        ):
            command = screen_command()
        rendered = " ".join(command)

        self.assertEqual(command[:3], ["/usr/bin/screen", "-dmS", SCREEN_NAME])
        self.assertIn(str(PROJECT_ROOT), rendered)
        self.assertIn(str(WRAPPER_PATH), rendered)
        self.assertIn(str(LOG_PATH), rendered)
        self.assertIn("QA_BACKEND_PREVENT_IDLE_SLEEP=true", rendered)
        self.assertIn("QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED=true", rendered)
        self.assertNotIn("TOKEN", rendered)
        self.assertNotIn("API_KEY", rendered)
        self.assertTrue(HEALTH_URL.endswith("/api/health/ready"))

    def test_prevent_idle_sleep_environment_value_takes_precedence(self):
        with patch.dict(
            os.environ,
            {"QA_BACKEND_PREVENT_IDLE_SLEEP": "true"},
        ):
            self.assertTrue(prevent_idle_sleep_enabled())

    def test_intraday_preflight_environment_value_takes_precedence(self):
        with patch.dict(
            os.environ,
            {"QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED": "true"},
        ):
            self.assertTrue(intraday_preflight_enabled())

    def test_start_refuses_foreign_port_listener(self):
        with (
            patch(
                "scripts.manage_research_shadow_supervisor.supervisor_pid",
                return_value=None,
            ),
            patch(
                "scripts.manage_research_shadow_supervisor.port_open",
                return_value=True,
            ),
            self.assertRaisesRegex(RuntimeError, "outside the managed supervisor"),
        ):
            start()

    def test_wrapper_launches_backend_with_private_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            created_path = temp_dir / "backend-created.txt"
            mode_path = temp_dir / "mode.txt"
            pid_file = temp_dir / "supervisor.pid"
            event_log = temp_dir / "events.jsonl"
            fake_backend.write_text(
                """import os
import stat
import time
from pathlib import Path

created = Path(os.environ["FAKE_CREATED_PATH"])
created.write_text("private\\n", encoding="ascii")
Path(os.environ["FAKE_MODE_PATH"]).write_text(
    oct(stat.S_IMODE(created.stat().st_mode)),
    encoding="ascii",
)
while True:
    time.sleep(1)
""",
                encoding="ascii",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_CREATED_PATH": str(created_path),
                    "FAKE_MODE_PATH": str(mode_path),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_HEALTH_URL": "http://127.0.0.1:1/ready",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "10",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "3",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "30",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": "",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "false",
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if mode_path.is_file():
                        break
                    if process.poll() is not None:
                        self.fail("supervisor exited before the fake backend started")
                    time.sleep(0.1)
                else:
                    self.fail("fake backend did not report its inherited file mode")
                self.assertEqual(mode_path.read_text(encoding="ascii"), "0o600")
                self.assertEqual(
                    stat.S_IMODE(pid_file.stat().st_mode),
                    0o600,
                )
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

            self.assertFalse(pid_file.exists())

    def test_stale_pid_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "supervisor.pid"
            path.write_text("999999\n", encoding="ascii")
            with (
                patch("scripts.manage_research_shadow_supervisor.PID_FILE", path),
                patch(
                    "scripts.manage_research_shadow_supervisor.process_command",
                    return_value="",
                ),
            ):
                pid = supervisor_pid()

            self.assertIsNone(pid)
            self.assertFalse(path.exists())

    def test_live_unrelated_pid_is_never_signaled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "supervisor.pid"
            path.write_text("42\n", encoding="ascii")
            with (
                patch("scripts.manage_research_shadow_supervisor.PID_FILE", path),
                patch(
                    "scripts.manage_research_shadow_supervisor.process_command",
                    return_value="python unrelated.py",
                ),
                self.assertRaisesRegex(RuntimeError, "unrelated live process"),
            ):
                supervisor_pid()

            self.assertTrue(path.exists())

    def test_stale_sleep_guard_pid_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sleep-guard.pid"
            path.write_text("999999\n", encoding="ascii")
            with (
                patch(
                    "scripts.manage_research_shadow_supervisor.SLEEP_GUARD_PID_FILE",
                    path,
                ),
                patch(
                    "scripts.manage_research_shadow_supervisor.process_command",
                    return_value="",
                ),
            ):
                pid = sleep_guard_pid(42)

            self.assertIsNone(pid)
            self.assertFalse(path.exists())

    def test_live_unrelated_sleep_guard_pid_is_never_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sleep-guard.pid"
            path.write_text("77\n", encoding="ascii")
            with (
                patch(
                    "scripts.manage_research_shadow_supervisor.SLEEP_GUARD_PID_FILE",
                    path,
                ),
                patch(
                    "scripts.manage_research_shadow_supervisor.process_command",
                    return_value="python unrelated.py",
                ),
                self.assertRaisesRegex(RuntimeError, "unrelated live process"),
            ):
                sleep_guard_pid(42)

            self.assertTrue(path.exists())

    def test_matching_caffeinate_process_is_accepted_as_sleep_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sleep-guard.pid"
            path.write_text("77\n", encoding="ascii")
            with (
                patch(
                    "scripts.manage_research_shadow_supervisor.SLEEP_GUARD_PID_FILE",
                    path,
                ),
                patch(
                    "scripts.manage_research_shadow_supervisor.process_command",
                    return_value="/usr/bin/caffeinate -i -w 42",
                ),
            ):
                self.assertEqual(sleep_guard_pid(42), 77)

    def test_wrapper_restarts_backend_after_repeated_health_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            starts_path = temp_dir / "starts.log"
            pid_file = temp_dir / "supervisor.pid"
            event_log = temp_dir / "events.jsonl"
            fake_backend.write_text(
                """import os
import time
from pathlib import Path

with Path(os.environ["FAKE_BACKEND_STARTS"]).open("a", encoding="ascii") as handle:
    handle.write("start\\n")

while True:
    time.sleep(1)
""",
                encoding="ascii",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_BACKEND_STARTS": str(starts_path),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_HEALTH_URL": "http://127.0.0.1:1/api/health",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "1",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "0",
                    "QA_BACKEND_RESTART_DELAY_SECONDS": "0",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": (
                        "http://127.0.0.1:2/hook-secret-value"
                    ),
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = ""
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if starts_path.is_file():
                        starts = starts_path.read_text(encoding="ascii").splitlines()
                        if len(starts) >= 2:
                            break
                    if process.poll() is not None:
                        self.fail("supervisor exited before restarting the fake backend")
                    time.sleep(0.2)
                else:
                    self.fail("supervisor did not restart the unhealthy fake backend")
            finally:
                process.terminate()
                try:
                    output = process.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:
                    process.kill()
                    output = process.communicate(timeout=5)[0]

            self.assertIn("backend health check failed", output)
            self.assertIn("restarting", output)
            self.assertIn("webhook:URLError", output)
            self.assertNotIn("hook-secret-value", output)
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["event"], "BACKEND_UNHEALTHY")
            self.assertFalse(pid_file.exists())

    def test_wrapper_emits_recovery_only_after_new_child_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            starts_path = temp_dir / "starts.log"
            pid_file = temp_dir / "supervisor.pid"
            event_log = temp_dir / "events.jsonl"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            fake_backend.write_text(
                """import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

starts = Path(os.environ["FAKE_BACKEND_STARTS"])
with starts.open("a", encoding="ascii") as handle:
    handle.write("start\\n")
if len(starts.read_text(encoding="ascii").splitlines()) == 1:
    raise SystemExit(7)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({"status": "healthy", "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass

ThreadingHTTPServer(("127.0.0.1", int(os.environ["FAKE_BACKEND_PORT"])), Handler).serve_forever()
""",
                encoding="ascii",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_BACKEND_STARTS": str(starts_path),
                    "FAKE_BACKEND_PORT": str(port),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_HEALTH_URL": f"http://127.0.0.1:{port}/ready",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "1",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "0",
                    "QA_BACKEND_RESTART_DELAY_SECONDS": "0",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": "",
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = ""
            try:
                deadline = time.monotonic() + 15
                events = []
                while time.monotonic() < deadline:
                    if event_log.is_file():
                        events = [
                            json.loads(line)
                            for line in event_log.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        ]
                        if [item["event"] for item in events] == [
                            "BACKEND_EXITED",
                            "BACKEND_RECOVERED",
                        ]:
                            break
                    if process.poll() is not None:
                        self.fail("supervisor exited before recovery was ready")
                    time.sleep(0.1)
                else:
                    self.fail(f"supervisor did not emit one recovery event: {events}")
            finally:
                process.terminate()
                try:
                    output = process.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:
                    process.kill()
                    output = process.communicate(timeout=5)[0]

            self.assertEqual(
                [item["event"] for item in events],
                ["BACKEND_EXITED", "BACKEND_RECOVERED"],
            )
            self.assertNotIn("BACKEND_UNHEALTHY", output)
            self.assertFalse(pid_file.exists())

    def test_wrapper_preflight_alerts_without_restarting_healthy_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            fake_preflight = temp_dir / "fake_preflight.py"
            starts_path = temp_dir / "starts.log"
            preflight_count_path = temp_dir / "preflight-count.txt"
            preflight_report_path = temp_dir / "preflight-report.json"
            pid_file = temp_dir / "supervisor.pid"
            event_log = temp_dir / "events.jsonl"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            fake_backend.write_text(
                """import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

with Path(os.environ["FAKE_BACKEND_STARTS"]).open("a", encoding="ascii") as handle:
    handle.write("start\\n")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({"status": "healthy", "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass

ThreadingHTTPServer(("127.0.0.1", int(os.environ["FAKE_BACKEND_PORT"])), Handler).serve_forever()
""",
                encoding="ascii",
            )
            fake_preflight.write_text(
                """import json
import os
from pathlib import Path

path = Path(os.environ["FAKE_PREFLIGHT_COUNT"])
count = int(path.read_text(encoding="ascii")) + 1 if path.exists() else 1
path.write_text(str(count), encoding="ascii")
ready = count >= 3
print(json.dumps({
    "version": 1,
    "status": "READY" if ready else "NOT_READY",
    "ready": ready,
    "checkedAt": f"attempt-{count}",
}))
raise SystemExit(0 if ready else 1)
""",
                encoding="ascii",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_BACKEND_STARTS": str(starts_path),
                    "FAKE_BACKEND_PORT": str(port),
                    "FAKE_PREFLIGHT_COUNT": str(preflight_count_path),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_HEALTH_URL": f"http://127.0.0.1:{port}/ready",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "1",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "0",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_ALERT_REPEAT_SECONDS": "30",
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": "",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "false",
                    "QA_BACKEND_INTRADAY_PREFLIGHT_ENABLED": "true",
                    "QA_BACKEND_INTRADAY_PREFLIGHT_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_INTRADAY_PREFLIGHT_SCRIPT": str(fake_preflight),
                    "QA_BACKEND_INTRADAY_PREFLIGHT_REPORT_FILE": str(
                        preflight_report_path
                    ),
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            events = []
            output = ""
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if event_log.is_file():
                        events = [
                            json.loads(line)
                            for line in event_log.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        ]
                        if [item["event"] for item in events] == [
                            "INTRADAY_PREFLIGHT_FAILED",
                            "INTRADAY_PREFLIGHT_RECOVERED",
                        ]:
                            break
                    if process.poll() is not None:
                        self.fail("supervisor exited during preflight recovery test")
                    time.sleep(0.1)
                else:
                    self.fail(f"supervisor did not recover preflight: {events}")
            finally:
                process.terminate()
                try:
                    output = process.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:
                    process.kill()
                    output = process.communicate(timeout=5)[0]

            self.assertEqual(
                [item["event"] for item in events],
                ["INTRADAY_PREFLIGHT_FAILED", "INTRADAY_PREFLIGHT_RECOVERED"],
            )
            self.assertEqual(starts_path.read_text(encoding="ascii").splitlines(), ["start"])
            self.assertGreaterEqual(
                int(preflight_count_path.read_text(encoding="ascii")), 3
            )
            report = json.loads(preflight_report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["ready"])
            self.assertEqual(report["status"], "READY")
            self.assertEqual(stat.S_IMODE(preflight_report_path.stat().st_mode), 0o600)
            self.assertNotIn("backend health check failed", output)
            self.assertFalse(pid_file.exists())

    def test_wrapper_uses_bounded_exponential_restart_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            starts_path = temp_dir / "starts.log"
            pid_file = temp_dir / "supervisor.pid"
            event_log = temp_dir / "events.jsonl"
            fake_backend.write_text(
                """import os
from pathlib import Path

with Path(os.environ["FAKE_BACKEND_STARTS"]).open("a", encoding="ascii") as handle:
    handle.write("start\\n")

raise SystemExit(7)
""",
                encoding="ascii",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_BACKEND_STARTS": str(starts_path),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_RESTART_DELAY_SECONDS": "1",
                    "QA_BACKEND_RESTART_MAX_DELAY_SECONDS": "2",
                    "QA_BACKEND_RESTART_RESET_SECONDS": "60",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "1",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "0",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": "",
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = ""
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if starts_path.is_file():
                        starts = starts_path.read_text(encoding="ascii").splitlines()
                        if len(starts) >= 3:
                            break
                    if process.poll() is not None:
                        self.fail("supervisor exited during restart backoff test")
                    time.sleep(0.1)
                else:
                    self.fail("supervisor did not perform three restart attempts")
            finally:
                process.terminate()
                try:
                    output = process.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:
                    process.kill()
                    output = process.communicate(timeout=5)[0]

            self.assertIn("restarting in 1s", output)
            self.assertIn("restarting in 2s", output)
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([item["event"] for item in events], ["BACKEND_EXITED"])
            self.assertFalse(pid_file.exists())

    def test_stable_child_resets_delay_before_exit_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            starts_path = temp_dir / "starts.log"
            pid_file = temp_dir / "supervisor.pid"
            event_log = temp_dir / "events.jsonl"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            fake_backend.write_text(
                """import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

starts = Path(os.environ["FAKE_BACKEND_STARTS"])
with starts.open("a", encoding="ascii") as handle:
    handle.write("start\\n")
start_count = len(starts.read_text(encoding="ascii").splitlines())
if start_count == 1:
    raise SystemExit(7)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({"status": "healthy", "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass

if start_count == 2:
    threading.Timer(2.5, lambda: os._exit(8)).start()
ThreadingHTTPServer(
    ("127.0.0.1", int(os.environ["FAKE_BACKEND_PORT"])), Handler
).serve_forever()
""",
                encoding="ascii",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_BACKEND_STARTS": str(starts_path),
                    "FAKE_BACKEND_PORT": str(port),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_HEALTH_URL": f"http://127.0.0.1:{port}/ready",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "1",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "0",
                    "QA_BACKEND_RESTART_DELAY_SECONDS": "1",
                    "QA_BACKEND_RESTART_MAX_DELAY_SECONDS": "2",
                    "QA_BACKEND_RESTART_RESET_SECONDS": "2",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": "",
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            events = []
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if event_log.is_file():
                        events = [
                            json.loads(line)
                            for line in event_log.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        ]
                        if [item["event"] for item in events] == [
                            "BACKEND_EXITED",
                            "BACKEND_RECOVERED",
                            "BACKEND_EXITED",
                            "BACKEND_RECOVERED",
                        ]:
                            break
                    if process.poll() is not None:
                        self.fail("supervisor exited during stable reset test")
                    time.sleep(0.1)
                else:
                    self.fail(f"supervisor did not complete stable reset: {events}")
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

            exit_events = [
                item for item in events if item["event"] == "BACKEND_EXITED"
            ]
            self.assertEqual(
                [item["restartDelaySeconds"] for item in exit_events], [1, 1]
            )
            self.assertFalse(pid_file.exists())

    def test_wrapper_owns_and_cleans_up_idle_sleep_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            fake_backend = temp_dir / "fake_backend.py"
            fake_caffeinate = temp_dir / "fake_caffeinate.sh"
            guard_args_path = temp_dir / "guard-args.log"
            pid_file = temp_dir / "supervisor.pid"
            guard_pid_file = temp_dir / "sleep-guard.pid"
            event_log = temp_dir / "events.jsonl"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            fake_backend.write_text(
                """import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({"status": "healthy", "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass

ThreadingHTTPServer(("127.0.0.1", int(os.environ["FAKE_BACKEND_PORT"])), Handler).serve_forever()
""",
                encoding="ascii",
            )
            fake_caffeinate.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" > "${FAKE_CAFFEINATE_ARGS}"
trap 'exit 0' INT TERM
while true; do
    sleep 1
done
""",
                encoding="ascii",
            )
            fake_caffeinate.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_BACKEND_PORT": str(port),
                    "FAKE_CAFFEINATE_ARGS": str(guard_args_path),
                    "QA_BACKEND_ENTRYPOINT": str(fake_backend),
                    "QA_BACKEND_SUPERVISOR_PID_FILE": str(pid_file),
                    "QA_BACKEND_HEALTH_URL": f"http://127.0.0.1:{port}/ready",
                    "QA_BACKEND_HEALTH_INTERVAL_SECONDS": "1",
                    "QA_BACKEND_HEALTH_TIMEOUT_SECONDS": "1",
                    "QA_BACKEND_HEALTH_FAILURE_THRESHOLD": "1",
                    "QA_BACKEND_STARTUP_GRACE_SECONDS": "0",
                    "QA_BACKEND_RESTART_DELAY_SECONDS": "0",
                    "QA_BACKEND_TERMINATION_GRACE_SECONDS": "1",
                    "QA_BACKEND_ALERT_EVENT_LOG": str(event_log),
                    "QA_BACKEND_DESKTOP_NOTIFICATIONS": "false",
                    "QA_BACKEND_ALERT_WEBHOOK_URL": "",
                    "QA_BACKEND_PREVENT_IDLE_SLEEP": "true",
                    "QA_BACKEND_CAFFEINATE_BIN": str(fake_caffeinate),
                    "QA_BACKEND_SLEEP_GUARD_PID_FILE": str(guard_pid_file),
                }
            )
            process = subprocess.Popen(
                ["/bin/bash", str(WRAPPER_PATH)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            guard_pid_value = None
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if (
                        pid_file.is_file()
                        and guard_pid_file.is_file()
                        and guard_args_path.is_file()
                    ):
                        break
                    if process.poll() is not None:
                        self.fail("supervisor exited before starting the sleep guard")
                    time.sleep(0.1)
                else:
                    self.fail("supervisor did not start the configured sleep guard")

                supervisor_pid_value = int(pid_file.read_text(encoding="ascii"))
                guard_pid_value = int(
                    guard_pid_file.read_text(encoding="ascii").strip()
                )
                self.assertEqual(
                    guard_args_path.read_text(encoding="ascii").strip(),
                    f"-i -w {supervisor_pid_value}",
                )
                os.kill(guard_pid_value, 0)
                guard_pid_file.unlink()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if guard_pid_file.is_file():
                        break
                    if process.poll() is not None:
                        self.fail("supervisor exited while repairing sleep guard state")
                    time.sleep(0.1)
                else:
                    self.fail("supervisor did not repair the sleep guard pid file")
                self.assertEqual(
                    int(guard_pid_file.read_text(encoding="ascii").strip()),
                    guard_pid_value,
                )
                os.kill(guard_pid_value, 0)
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

            self.assertFalse(pid_file.exists())
            self.assertFalse(guard_pid_file.exists())
            if guard_pid_value is not None:
                with self.assertRaises(ProcessLookupError):
                    os.kill(guard_pid_value, 0)


if __name__ == "__main__":
    unittest.main()
