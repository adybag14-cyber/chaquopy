import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from compat.hermes import build_native
from compat.hermes.tests import test_verify_app_report


class NativeBuildContractTests(unittest.TestCase):
    def test_one_explicit_python_and_abi_identifier(self):
        command = build_native.command_for(Path("source"), Path("wheels"), "3.14", "arm64-v8a")
        self.assertEqual(command[:3], [sys.executable, "-m", "cibuildwheel"])
        self.assertEqual(command[command.index("--only") + 1], "cp314-android_arm64_v8a")
        self.assertIn("--config-file", command)
        self.assertEqual(command[command.index("--output-dir") + 1], "wheels")

    def test_host_credentials_and_ambient_skip_settings_do_not_leak(self):
        with patch.dict(os.environ, {
            "GITHUB_TOKEN": "do-not-forward", "SERVICE_API_KEY": "do-not-forward",
            "CIBW_TEST_SKIP": "*", "CIBW_TEST_COMMAND_ANDROID": "echo fake",
            "ANDROID_HOME": "/sdk",
        }, clear=True):
            environment = build_native.build_environment("jiter")
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("SERVICE_API_KEY", environment)
        self.assertEqual(environment["ANDROID_HOME"], "/sdk")
        self.assertEqual(environment["CIBW_TEST_SKIP"], "")
        self.assertEqual(
            environment["CIBW_TEST_COMMAND_ANDROID"],
            "python -m compat.hermes.runtime_smoke --package jiter --require-android --log-marker",
        )
        self.assertIn("ANDROID_API_LEVEL=24", environment["CIBW_ENVIRONMENT_ANDROID"])
        self.assertIn("MATURIN_PEP517_ARGS=--locked", environment["CIBW_ENVIRONMENT_ANDROID"])
        self.assertIn("max-page-size=16384", environment["CIBW_ENVIRONMENT_ANDROID"])

    def test_runtime_receipt_preserves_actual_api_and_hash(self):
        measured = test_verify_app_report.AppReportTests().report("jiter", api=27)
        measured["platform"]["page_size"] = 4096
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build_native.record_runtime_evidence(
                report, [measured], output, python="3.14", abi="x86_64",
                package="jiter", host_machine="x86_64",
            )
            raw = (output / "runtime-report.json").read_bytes()
        self.assertEqual(json.loads(raw), measured)
        self.assertEqual(report["runtime_report_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(report["runtime_android_api"], 27)
        self.assertEqual(report["runtime_page_size"], 4096)
        self.assertEqual(report["runtime_python"], "3.14.0")
        self.assertEqual(report["runtime_validation"], "cibuildwheel-android-testbed-passed")

    def test_missing_duplicate_and_invalid_runtime_receipts_fail_closed(self):
        measured = test_verify_app_report.AppReportTests().report("jiter")
        for reports in ([], [measured, measured], [{"status": "passed"}]):
            with self.subTest(reports=reports), tempfile.TemporaryDirectory() as directory:
                report = {}
                output = Path(directory)
                with self.assertRaises(ValueError):
                    build_native.record_runtime_evidence(
                        report, reports, output, python="3.14", abi="x86_64",
                        package="jiter", host_machine="x86_64",
                    )
                self.assertNotIn("runtime_validation", report)
                self.assertFalse((output / "runtime-report.json").exists())

    def test_cross_architecture_is_not_runtime_certified(self):
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build_native.record_runtime_evidence(
                report, [], output, python="3.14", abi="arm64-v8a",
                package="jiter", host_machine="x86_64",
            )
            self.assertEqual(report, {"runtime_validation": "not_run_cross_architecture"})
            self.assertFalse((output / "runtime-report.json").exists())
            with self.assertRaisesRegex(ValueError, "cross-architecture"):
                build_native.record_runtime_evidence(
                    {}, [{"status": "passed"}], output, python="3.14", abi="arm64-v8a",
                    package="jiter", host_machine="x86_64",
                )
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                build_native.record_runtime_evidence(
                    {}, [], output, python="3.14", abi="x86_64",
                    package="jiter", host_machine="ppc64",
                )


@unittest.skipUnless(os.name == "posix", "Android build drivers run on Linux/macOS")
class LoggedProcessTests(unittest.TestCase):
    def run_script(self, script, timeout=10):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build.log"
            with contextlib.redirect_stdout(io.StringIO()):
                reports = build_native.run_logged(
                    [sys.executable, "-c", script], os.environ.copy(), timeout, path,
                )
            return reports, path.read_text(encoding="utf-8")

    def test_actual_marker_is_parsed_and_full_log_preserved(self):
        reports, log = self.run_script(
            "print('compiler output'); "
            "print('I python.stdout: HERMES_RUNTIME_SMOKE_JSON={\"measured\": 27}')"
        )
        self.assertEqual(reports, [{"measured": 27}])
        self.assertIn("compiler output\n", log)
        self.assertIn("HERMES_RUNTIME_SMOKE_JSON=", log)

    def test_nonzero_exit_is_never_treated_as_success(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_script("raise SystemExit(7)")
        self.assertEqual(error.exception.returncode, 7)

    def test_malformed_runtime_report_is_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            self.run_script("print('HERMES_RUNTIME_SMOKE_JSON=not-json')")

    def test_timeout_stops_only_the_new_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeout.log"
            command = [sys.executable, "-c", "import os,time; print(os.getpid(),flush=True); "
                       "time.sleep(20)"]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(
                subprocess.TimeoutExpired
            ):
                build_native.run_logged(command, os.environ.copy(), 0.5, path)
            child_pid = int(path.read_text(encoding="utf-8").strip())
            self.assertNotEqual(child_pid, os.getpid())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_timeout_handles_exited_leader_with_child_holding_stdout(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_script(
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])",
                timeout=0.5,
            )
        self.assertLess(time.monotonic() - started, 8)


if __name__ == "__main__":
    unittest.main()
