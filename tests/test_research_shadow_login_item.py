import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.manage_research_shadow_login_item import (
    APP_NAME,
    APP_BUILD_VERSION,
    BUNDLE_ID,
    authorization_confirmed,
    build_app,
    current_boot_launch_confirmed,
    install,
    launcher_authorization_token,
    launcher_source,
    login_item_registered,
    record_login_launch_receipt,
    register_login_item,
    source_sha256,
    verify_app_bundle,
)


class ResearchShadowLoginItemTests(unittest.TestCase):
    @staticmethod
    def _valid_receipt(*, boot_session_sha256: str = "a" * 64):
        return {
            "version": 1,
            "recordedAt": "2026-08-14T23:00:00+08:00",
            "authorizationToken": launcher_authorization_token(),
            "appBuildVersion": APP_BUILD_VERSION,
            "launcherSourceSha256": source_sha256(launcher_source()),
            "bootSessionSha256": boot_session_sha256,
            "supervisorPid": os.getpid(),
            "backendPid": 123,
            "backendInstanceId": "instance",
            "backendReady": True,
            "workersReady": True,
        }

    def test_launcher_uses_existing_manager_without_secrets(self):
        source = launcher_source(
            python_bin=Path("/safe/python"),
            manager_path=Path("/safe/project/manage.py"),
            login_manager_path=Path("/safe/project/login.py"),
            log_path=Path("/safe/logs/startup.log"),
            auth_marker=Path("/safe/state/authorized"),
            start_timeout_seconds=7,
        )

        self.assertIn("/safe/python", source)
        self.assertIn("/safe/project/manage.py", source)
        self.assertIn("/safe/project/login.py", source)
        self.assertIn("confirm-launch", source)
        self.assertIn(" start ", source)
        self.assertIn("umask 077", source)
        self.assertIn("/safe/state", source)
        self.assertIn('attempt\\\" -ge 7', source)
        self.assertIn("kill -TERM", source)
        self.assertIn("kill -KILL", source)
        self.assertNotIn("TOKEN", source)
        self.assertNotIn("API_KEY", source)
        self.assertNotIn(".env", source)

    def test_build_app_has_stable_identity_and_documents_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / f"{APP_NAME}.app"
            built = build_app(app_path)
            plist_path = built / "Contents" / "Info.plist"
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)

            self.assertEqual(plist["CFBundleIdentifier"], BUNDLE_ID)
            self.assertEqual(plist["CFBundleDisplayName"], APP_NAME)
            self.assertTrue(plist["NSDocumentsFolderUsageDescription"])
            self.assertEqual(plist["QuantaAlphaLauncherBuildVersion"], APP_BUILD_VERSION)
            self.assertNotIn("NSCameraUsageDescription", plist)
            self.assertNotIn("NSMicrophoneUsageDescription", plist)
            self.assertNotIn("NSContactsUsageDescription", plist)
            self.assertNotIn("NSAppleEventsUsageDescription", plist)
            self.assertEqual(len(plist["QuantaAlphaLauncherSourceSha256"]), 64)
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(built)],
                check=True,
            )
            self.assertTrue(verify_app_bundle(built)["valid"])

    def test_build_app_replaces_a_tampered_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / f"{APP_NAME}.app"
            build_app(app_path)
            main_script = (
                app_path / "Contents" / "Resources" / "Scripts" / "main.scpt"
            )
            main_script.write_bytes(b"tampered")

            verification = verify_app_bundle(app_path)
            self.assertFalse(verification["valid"])
            self.assertIn("codeSignatureValid", verification["failures"])
            self.assertIn("decompiledLauncherMatches", verification["failures"])

            build_app(app_path)
            self.assertTrue(verify_app_bundle(app_path)["valid"])

    def test_authorization_marker_is_bound_to_current_launcher_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "authorized"
            marker.write_text("stale-token\n", encoding="ascii")
            self.assertFalse(authorization_confirmed(marker))

            marker.write_text(
                json.dumps(self._valid_receipt()) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(authorization_confirmed(marker))

    def test_current_boot_confirmation_rejects_an_old_boot_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "authorized"
            marker.write_text(
                json.dumps(self._valid_receipt(boot_session_sha256="a" * 64)),
                encoding="utf-8",
            )

            with patch(
                "scripts.manage_research_shadow_login_item.current_boot_session_sha256",
                return_value="a" * 64,
            ):
                self.assertTrue(current_boot_launch_confirmed(marker))
            with patch(
                "scripts.manage_research_shadow_login_item.current_boot_session_sha256",
                return_value="b" * 64,
            ):
                self.assertFalse(current_boot_launch_confirmed(marker))

    def test_launch_receipt_requires_token_integrity_and_ready_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "authorized"
            pid_file = root / "supervisor.pid"
            pid_file.write_text(str(os.getpid()), encoding="ascii")
            ready_worker = {
                "ready": True,
                "taskAlive": True,
                "consecutiveErrors": 0,
            }
            health = {
                "status": "healthy",
                "ready": True,
                "pid": 123,
                "instanceId": "instance",
                "workers": {
                    "researchShadowWatcher": ready_worker,
                    "intradayCapture": ready_worker,
                    "intradayShadow": ready_worker,
                },
            }

            with self.assertRaisesRegex(RuntimeError, "token does not match"):
                record_login_launch_receipt(
                    "wrong",
                    marker=marker,
                    supervisor_pid_file=pid_file,
                )

            with (
                patch(
                    "scripts.manage_research_shadow_login_item.verify_app_bundle",
                    return_value={"valid": True, "failures": []},
                ),
                patch(
                    "scripts.manage_research_shadow_login_item._backend_health",
                    return_value={"status": "healthy", "ready": True, "workers": {}},
                ),
                self.assertRaisesRegex(RuntimeError, "workers are not ready"),
            ):
                record_login_launch_receipt(
                    launcher_authorization_token(),
                    marker=marker,
                    supervisor_pid_file=pid_file,
                )

            with (
                patch(
                    "scripts.manage_research_shadow_login_item.verify_app_bundle",
                    return_value={"valid": True, "failures": []},
                ),
                patch(
                    "scripts.manage_research_shadow_login_item._backend_health",
                    return_value=health,
                ),
                patch(
                    "scripts.manage_research_shadow_login_item.current_boot_session_sha256",
                    return_value="a" * 64,
                ),
            ):
                receipt = record_login_launch_receipt(
                    launcher_authorization_token(),
                    marker=marker,
                    supervisor_pid_file=pid_file,
                )

            self.assertTrue(authorization_confirmed(marker))
            self.assertEqual(receipt["backendInstanceId"], "instance")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_login_item_registration_is_hidden_and_idempotent(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        )
        app_path = Path("/Applications/QuantaAlpha Test.app")
        register_login_item(app_path, run=runner)

        script = runner.call_args.args[0][-1]
        self.assertIn(f'if exists login item "{APP_NAME}" then delete', script)
        self.assertIn("hidden:true", script)
        self.assertIn(str(app_path), script)

    def test_login_item_status_parses_system_events_boolean(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, stdout="true\n", stderr="")
        )
        self.assertTrue(login_item_registered(run=runner))

    def test_launcher_timeout_reaps_blocked_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "blocked-manager.sh"
            pid_file = root / "manager.pid"
            marker = root / "authorized"
            manager.write_text(
                "#!/bin/sh\n"
                f"printf '%s' \"$$\" > {pid_file}\n"
                "while :; do :; done\n",
                encoding="utf-8",
            )
            manager.chmod(0o700)
            source = launcher_source(
                python_bin=Path("/bin/sh"),
                manager_path=manager,
                log_path=root / "startup.log",
                auth_marker=marker,
                start_timeout_seconds=1,
            )

            result = subprocess.run(
                ["/usr/bin/osascript", "-e", source],
                check=False,
                text=True,
                capture_output=True,
                timeout=5,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            pid = int(pid_file.read_text(encoding="ascii"))
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"blocked login manager remained alive: {pid}")

    def test_install_refuses_registration_without_authorization_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / f"{APP_NAME}.app"
            marker = Path(tmp) / "authorized"
            with (
                patch(
                    "scripts.manage_research_shadow_login_item.build_app",
                    return_value=app_path,
                ),
                patch("scripts.manage_research_shadow_login_item.AUTH_MARKER", marker),
                patch("scripts.manage_research_shadow_login_item._run"),
                patch(
                    "scripts.manage_research_shadow_login_item.register_login_item"
                ) as register,
                self.assertRaisesRegex(RuntimeError, "did not confirm repository access"),
            ):
                install()

            register.assert_not_called()

    def test_install_registers_only_after_authorization_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / f"{APP_NAME}.app"
            marker = Path(tmp) / "authorized"

            def confirm_access(*_args, **_kwargs):
                marker.write_text(
                    json.dumps(self._valid_receipt()) + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                patch(
                    "scripts.manage_research_shadow_login_item.build_app",
                    return_value=app_path,
                ),
                patch("scripts.manage_research_shadow_login_item.AUTH_MARKER", marker),
                patch(
                    "scripts.manage_research_shadow_login_item._run",
                    side_effect=confirm_access,
                ),
                patch(
                    "scripts.manage_research_shadow_login_item.register_login_item"
                ) as register,
            ):
                install()

            register.assert_called_once_with(app_path)


if __name__ == "__main__":
    unittest.main()
