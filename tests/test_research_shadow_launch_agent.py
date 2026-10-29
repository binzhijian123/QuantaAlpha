import io
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts.manage_research_shadow_launch_agent import (
    LABEL,
    PROJECT_ROOT,
    STDERR_PATH,
    STDOUT_PATH,
    WRAPPER_PATH,
    build_plist,
    install,
    launch_target,
    main,
    render_plist,
    startup_failure_message,
    status_payload,
)


def _result(returncode=0):
    return subprocess.CompletedProcess(["launchctl"], returncode, "", "")


class ResearchShadowLaunchAgentTests(unittest.TestCase):
    def test_plist_has_one_keepalive_backend_without_secrets(self):
        payload = build_plist()

        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["ProgramArguments"], ["/bin/bash", str(WRAPPER_PATH)])
        self.assertEqual(payload["WorkingDirectory"], str(PROJECT_ROOT))
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertGreaterEqual(payload["ThrottleInterval"], 10)
        self.assertEqual(payload["StandardOutPath"], str(STDOUT_PATH))
        self.assertEqual(payload["StandardErrorPath"], str(STDERR_PATH))
        rendered = render_plist()
        self.assertEqual(plistlib.loads(rendered), payload)
        self.assertNotIn(b"TOKEN", rendered)
        self.assertNotIn(b"API_KEY", rendered)

    def test_launch_target_is_scoped_to_current_gui_user(self):
        self.assertEqual(
            launch_target(501),
            f"gui/501/{LABEL}",
        )

    def test_install_refuses_foreign_port_listener_before_writing_plist(self):
        calls = []

        def run_launchctl(args, check=True):
            calls.append((list(args), check))
            return _result(returncode=1 if args[0] == "print" else 0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.plist"
            with self.assertRaisesRegex(RuntimeError, "port 8000"):
                install(
                    path,
                    run_launchctl=run_launchctl,
                    is_port_open=lambda: True,
                )

            self.assertFalse(path.exists())
            self.assertEqual(calls, [(["print", launch_target()], False)])

    def test_install_bootstraps_valid_plist_when_port_is_free(self):
        calls = []

        def run_launchctl(args, check=True):
            calls.append(list(args))
            return _result(returncode=1 if args[0] == "print" else 0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.plist"
            with (
                patch(
                    "scripts.manage_research_shadow_launch_agent.health_ok",
                    return_value=True,
                ),
                patch(
                    "scripts.manage_research_shadow_launch_agent.subprocess.run"
                ) as validate,
            ):
                validate.return_value = _result()
                install(
                    path,
                    run_launchctl=run_launchctl,
                    is_port_open=lambda: False,
                )

            self.assertEqual(plistlib.loads(path.read_bytes()), build_plist())
            self.assertIn(["bootstrap", launch_target().rsplit("/", 1)[0], str(path)], calls)
            self.assertIn(["enable", launch_target()], calls)
            self.assertIn(["kickstart", "-k", launch_target()], calls)

    def test_cli_reports_runtime_error_without_traceback(self):
        error = io.StringIO()
        with (
            patch("sys.argv", ["manager", "install"]),
            patch(
                "scripts.manage_research_shadow_launch_agent.install",
                side_effect=RuntimeError("port conflict"),
            ),
            redirect_stderr(error),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(error.getvalue(), "Error: port conflict\n")

    def test_failed_health_check_boots_out_and_removes_plist(self):
        calls = []

        def run_launchctl(args, check=True):
            calls.append((list(args), check))
            return _result(returncode=1 if args[0] == "print" else 0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.plist"
            with (
                patch(
                    "scripts.manage_research_shadow_launch_agent.wait_until",
                    return_value=False,
                ),
                patch(
                    "scripts.manage_research_shadow_launch_agent.startup_failure_message",
                    return_value="health failed",
                ),
                patch(
                    "scripts.manage_research_shadow_launch_agent.subprocess.run"
                ) as validate,
            ):
                validate.return_value = _result()
                with self.assertRaisesRegex(RuntimeError, "health failed"):
                    install(
                        path,
                        run_launchctl=run_launchctl,
                        is_port_open=lambda: False,
                    )

            self.assertFalse(path.exists())
            self.assertIn((["bootout", launch_target()], False), calls)

    def test_permission_failure_has_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            error_path = Path(tmp) / "stderr.log"
            error_path.write_text(
                "/bin/bash: project/script.sh: Operation not permitted\n",
                encoding="utf-8",
            )
            with patch(
                "scripts.manage_research_shadow_launch_agent.STDERR_PATH",
                error_path,
            ):
                message = startup_failure_message()

        self.assertIn("macOS denied LaunchAgent access", message)
        self.assertIn("supervised Screen", message)

    def test_status_distinguishes_foreign_backend_health(self):
        with (
            patch(
                "scripts.manage_research_shadow_launch_agent._run_launchctl",
                return_value=_result(returncode=1),
            ),
            patch(
                "scripts.manage_research_shadow_launch_agent.health_ok",
                return_value=True,
            ),
        ):
            payload = status_payload()

        self.assertFalse(payload["loaded"])
        self.assertFalse(payload["managedHealthy"])
        self.assertTrue(payload["backendHealthy"])


if __name__ == "__main__":
    unittest.main()
