"""Dependency-free regression tests for the evidence runner's fail-closed contract."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "hermes_runtime_smoke_contract_target", Path(__file__).resolve().parents[1] / "runtime_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def host_evidence():
    return {
        "implementation": "CPython", "python": "3.14.0", "platform": "win32",
        "is_android": False, "android_api_level": None,
    }


class RuntimeSmokeContractTests(unittest.TestCase):
    def test_android_requirement_rejects_host_before_dependencies(self):
        with mock.patch.object(smoke, "platform_evidence", return_value=host_evidence()), \
                mock.patch.object(smoke, "package_evidence") as packages:
            report = smoke.run("jiter", require_android=True)
        self.assertEqual(report["status"], "failed")
        self.assertIn("rejects host-only execution", report["preflight_error"])
        self.assertFalse(report["android_execution_verified"])
        packages.assert_not_called()

    def test_android_identity_needs_platform_and_api(self):
        with mock.patch.object(sys, "platform", "android"), \
                mock.patch.object(sys, "getandroidapilevel", return_value=24, create=True), \
                mock.patch.object(smoke.platform, "system", return_value="Android"), \
                mock.patch.object(smoke.platform, "machine", return_value="aarch64"), \
                mock.patch.object(smoke.sysconfig, "get_config_var", return_value="fixture-soabi"):
            self.assertTrue(smoke.platform_evidence()["is_android"])
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(sys, "getandroidapilevel", return_value=24, create=True), \
                mock.patch.object(smoke.sysconfig, "get_config_var", return_value="fixture-soabi"):
            self.assertFalse(smoke.platform_evidence()["is_android"])

    def test_unsupported_python_and_android_api_fail(self):
        for version in ("3.12.10", "3.15.0"):
            evidence = {**host_evidence(), "python": version}
            with (self.subTest(version=version),
                  self.assertRaisesRegex(RuntimeError, "3.13 and 3.14")):
                smoke.validate_platform(evidence, False)
        evidence = {**host_evidence(), "is_android": True, "android_api_level": 23}
        with self.assertRaisesRegex(RuntimeError, "API 24"):
            smoke.validate_platform(evidence, True)

    def test_page_size_comes_from_runtime_not_assumed_android_abi(self):
        for page_size in (4096, 16384):
            with (self.subTest(page_size=page_size),
                  mock.patch.object(smoke.os, "sysconf", return_value=page_size, create=True)
                  as sysconf):
                self.assertEqual(smoke.platform_evidence()["page_size"], page_size)
                sysconf.assert_called_once_with("SC_PAGE_SIZE")
        with mock.patch.object(smoke.os, "sysconf", None, create=True):
            self.assertIsNone(smoke.platform_evidence()["page_size"])

    def test_package_version_mismatch_is_not_hidden(self):
        with mock.patch.object(smoke.importlib.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(RuntimeError, "expected 0.16.0, installed 0.0.0"):
                smoke.package_evidence("jiter")

    def test_fixture_assertions_are_explicit(self):
        with self.assertRaisesRegex(AssertionError, "bad fixture"):
            smoke.require_fixture(False, "bad fixture")
        smoke.require_fixture(True, "good fixture")

    def test_native_module_rejects_python_shim(self):
        with mock.patch.object(smoke.importlib, "import_module", return_value=types.SimpleNamespace(
            __file__="/somewhere/jiter.py"
        )):
            with self.assertRaisesRegex(RuntimeError, "not a native extension"):
                smoke.module_evidence("jiter.jiter", require_native=True)

    def test_missing_package_fails_without_running_behavior_suite(self):
        with (
            mock.patch.object(smoke, "platform_evidence", return_value=host_evidence()),
            mock.patch.object(smoke, "package_evidence", side_effect=ModuleNotFoundError("jiter")),
            mock.patch.object(smoke, "build_suite") as suite,
        ):
            report = smoke.run("jiter")
        self.assertEqual(report["status"], "failed")
        self.assertIn("ModuleNotFoundError", report["preflight_error"])
        suite.assert_not_called()

    def test_full_lane_has_all_required_real_distributions(self):
        self.assertEqual(set(smoke.PACKAGE_GROUPS["full"]), {
            "jiter", "pydantic-core", "pydantic", "msgpack", "aiohttp", "openai",
            "anthropic", "fal-client", "httpx",
        })
        self.assertEqual(smoke.PACKAGE_SPECS["pydantic"][0], "2.13.5")
        self.assertEqual(smoke.PACKAGE_SPECS["pydantic-core"][0], "2.46.5")
        self.assertEqual(smoke.build_suite("jiter").countTestCases(), 3)
        self.assertEqual(smoke.build_suite("pydantic-core").countTestCases(), 2)
        self.assertGreaterEqual(smoke.build_suite("full").countTestCases(), 23)

    def test_sdk_streaming_cases_stay_in_full_lane_only(self):
        cases = (smoke.OpenAIResponsesAndStreamsTests, smoke.OpenAIAsyncResponsesAndStreamsTests)
        for case in cases:
            self.assertIn(case, smoke.TEST_GROUPS["full"])
            self.assertNotIn(case, smoke.TEST_GROUPS["jiter"])
            self.assertNotIn(case, smoke.TEST_GROUPS["pydantic-core"])
            names = unittest.defaultTestLoader.getTestCaseNames(case)
            self.assertIn("test_responses_create", names)
            self.assertIn("test_responses_sse_create_and_stream_helper", names)
            self.assertIn("test_chat_sse_raw_and_structured_helper", names)
            self.assertIn("test_responses_helper_rejects_missing_completion", names)

    def test_sse_fixture_encodes_named_utf8_events_and_done(self):
        event = {"type": "response.output_text.delta", "delta": "\u03bb"}
        body = smoke.sse_bytes([event], named=True, done=True)
        self.assertTrue(body.startswith(b": hermes offline fixture\r\n\r\n"))
        self.assertIn(b"event: response.output_text.delta\r\n", body)
        self.assertIn("\u03bb".encode("utf-8"), body)
        self.assertTrue(body.endswith(b"data: [DONE]\r\n\r\n"))

    def test_response_fixture_missing_completion_is_not_synthesized(self):
        events = smoke.response_fixture_events(completed=False)
        self.assertFalse(any(event["type"] == "response.completed" for event in events))
        self.assertEqual(events[0]["response"]["status"], "in_progress")
        self.assertEqual(events[0]["response"]["output"], [])
        complete = smoke.response_fixture_events()
        self.assertEqual(complete[-1]["type"], "response.completed")
        self.assertEqual([event["sequence_number"] for event in complete], list(range(10)))

    def test_preloaded_native_msgpack_fails_instead_of_silent_fallback(self):
        loaded = types.SimpleNamespace(Packer=types.SimpleNamespace(__module__="msgpack._cmsgpack"))
        with mock.patch.dict(sys.modules, {"msgpack": loaded}):
            with self.assertRaisesRegex(RuntimeError, "fresh interpreter"):
                smoke.prepare_msgpack()

    def test_host_success_is_never_android_or_service_evidence(self):
        class PassingCase(unittest.TestCase):
            def runTest(self):
                self.assertTrue(True)

        with (
            mock.patch.object(smoke, "platform_evidence", return_value=host_evidence()),
            mock.patch.object(smoke, "package_evidence", return_value={"fixture": True}),
            mock.patch.object(
                smoke, "build_suite", return_value=unittest.TestSuite([PassingCase()])
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            report = smoke.run("jiter")
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["android_execution_verified"])
        self.assertFalse(report["service_inference_verified"])
        self.assertEqual(len(report["tests"]["passed"]), 1)

    def test_skipped_failed_and_empty_suites_cannot_pass(self):
        class SkippedCase(unittest.TestCase):
            def runTest(self):
                self.skipTest("missing dependency")

        class FailingCase(unittest.TestCase):
            def runTest(self):
                self.fail("regression")

        for tests in ([], [SkippedCase()], [FailingCase()]):
            with (
                self.subTest(tests=tests),
                mock.patch.object(smoke, "platform_evidence", return_value=host_evidence()),
                mock.patch.object(smoke, "package_evidence", return_value={"fixture": True}),
                mock.patch.object(smoke, "build_suite", return_value=unittest.TestSuite(tests)),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(smoke.run("jiter")["status"], "failed")

    def test_main_writes_matching_json_and_nonzero_failure(self):
        report = {"status": "failed", "preflight_error": "fixture failure"}
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nested" / "evidence.json"
            with mock.patch.object(smoke, "run", return_value=report) as runner, \
                    contextlib.redirect_stdout(stdout):
                code = smoke.main([
                    "--package", "jiter", "--require-android", "--json-output", str(target),
                ])
            self.assertEqual(json.loads(target.read_text()), report)
        runner.assert_called_once_with("jiter", True)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
