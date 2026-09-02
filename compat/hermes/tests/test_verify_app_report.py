"""Adversarial checks of receipt validation; fixtures are not runtime evidence."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from compat.hermes import runtime_smoke, verify_app_report as verifier


class AppReportTests(unittest.TestCase):
    def report(self, group="full", python="3.14", abi="x86_64", api=35):
        soabi = f"cpython-{python.replace('.', '')}-{verifier.TRIPLETS[abi]}"
        packages = []
        for name in runtime_smoke.PACKAGE_GROUPS[group]:
            version, module, native = runtime_smoke.PACKAGE_SPECS[name]
            packages.append({
                "distribution": name, "version": version, "expected_version": version,
                "module": {"module": module, "native": False,
                           "file": f"/data/fixture/{module.replace('.', '/')}/__init__.py"},
                "native_modules": [{
                    "module": native_name, "native": True,
                    "file": "/data/fixture/" + native_name.replace(".", "/") + f".{soabi}.so",
                } for native_name in native],
            })
        cases = sorted(verifier.expected_cases(group))
        return {
            "schema_version": 1, "status": "passed", "package_group": group,
            "android_execution_verified": True, "require_android": True,
            "platform": {
                "platform": "android", "is_android": True, "python": python + ".0",
                "implementation": "CPython", "soabi": soabi,
                "machine": "aarch64" if abi == "arm64-v8a" else "x86_64",
                "android_api_level": api, "page_size": 16384,
            },
            "tests": {"run": len(cases), "passed": ["runtime_smoke." + case for case in cases],
                      "failures": [], "errors": [], "skipped": []},
            "packages": packages,
            "network_scope": "loopback-and-in-memory-fixtures-only",
            "service_inference_verified": False,
        }

    def test_accept_exact_full_and_native_lanes(self):
        for group in runtime_smoke.PACKAGE_GROUPS:
            for python in ("3.13", "3.14"):
                for abi in verifier.MACHINES:
                    for api in (24, 27, 35):
                        with self.subTest(group=group, python=python, abi=abi, api=api):
                            verifier.validate(self.report(group, python, abi, api), python, abi,
                                              package_group=group, expected_api=api)

    def test_verification_never_executes_tests_or_imports_dependencies(self):
        report = self.report()
        with (
            mock.patch.object(runtime_smoke, "run", side_effect=AssertionError("must not run")),
            mock.patch.object(runtime_smoke, "package_evidence",
                              side_effect=AssertionError("must not import")),
            mock.patch.object(runtime_smoke.JiterTests, "test_valid_json",
                              side_effect=AssertionError("must not execute")),
        ):
            verifier.validate(report, "3.14", "x86_64")

    def test_reject_missing_packages_wrong_version_and_unknown_schema(self):
        for change in ({"packages": []}, {"schema_version": 99}, {"schema_version": True},
                       {"require_android": False}, {"preflight_error": "load failure"},
                       {"package_group": "jiter"}, {"status": "failed"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verifier.validate(self.report() | change, "3.14", "x86_64")
        report = self.report()
        report["packages"][0]["version"] = "0.0.0"
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            verifier.validate(report, "3.14", "x86_64")

    def test_reject_one_test_duplicates_missing_and_unknown_cases(self):
        count = self.report()["tests"]["run"]
        mutations = [
            {"run": 1, "passed": ["test"]},
            {"passed": ["invented.Test.test"] * count},
            {"passed": self.report()["tests"]["passed"][:-1]},
            {"run": True},
            {"passed": [None] * count},
        ]
        for change in mutations:
            report = self.report()
            report["tests"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64")
        report = self.report()
        duplicate = report["tests"]["passed"][1].replace("runtime_smoke", "alias")
        report["tests"]["passed"][0] = duplicate
        with self.assertRaisesRegex(ValueError, "duplicate"):
            verifier.validate(report, "3.14", "x86_64")

    def test_normalized_case_names_allow_main_and_module_import(self):
        report = self.report("jiter")
        report["tests"]["passed"] = [
            "__main__." + case for case in sorted(verifier.expected_cases("jiter"))
        ]
        verifier.validate(report, "3.14", "x86_64", package_group="jiter")
        report["tests"]["passed"] = [
            "compat.hermes.runtime_smoke." + case
            for case in sorted(verifier.expected_cases("jiter"))
        ]
        verifier.validate(report, "3.14", "x86_64", package_group="jiter")

    def test_reject_host_wrong_target_skips_and_api_mismatch(self):
        for section, key, value in (
            ("platform", "platform", "linux"), ("platform", "machine", "aarch64"),
            ("platform", "implementation", "PyPy"), ("platform", "soabi", "cp314-win_amd64"),
            ("platform", "python", "3.13.9"), ("platform", "python", "3.14.fake"),
            ("platform", "page_size", None), ("platform", "page_size", True),
            ("platform", "android_api_level", True), ("platform", "android_api_level", 23),
            ("tests", "run", 0), ("tests", "skipped", ["missing wheel"]),
            ("tests", "failures", ["bad result"]), ("tests", "errors", ["import failure"]),
        ):
            report = self.report()
            report[section][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64")
        with self.assertRaisesRegex(ValueError, "exact Android API"):
            verifier.validate(self.report(api=27), "3.14", "x86_64", expected_api=24)

    def test_reject_duplicate_and_unexpected_package_records(self):
        for mode in ("duplicate", "extra", "missing", "bad-shape"):
            report = self.report()
            if mode == "duplicate":
                report["packages"][1] = copy.deepcopy(report["packages"][0])
            elif mode == "extra":
                report["packages"].append(copy.deepcopy(report["packages"][0]))
            elif mode == "missing":
                report["packages"].pop()
            else:
                report["packages"][0] = None
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64")

    def test_reject_missing_wrong_or_duplicate_native_provenance(self):
        for value in ([], [None], [{
            "module": "jiter.jiter", "native": False, "file": "/data/jiter/jiter.py",
        }]):
            report = self.report()
            report["packages"][0]["native_modules"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64")
        report = self.report()
        aiohttp = next(record for record in report["packages"]
                       if record["distribution"] == "aiohttp")
        aiohttp["native_modules"][1] = copy.deepcopy(aiohttp["native_modules"][0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            verifier.validate(report, "3.14", "x86_64")

    def test_reject_non_android_or_wrong_target_native_paths(self):
        for path in ("C:\\fake\\jiter.pyd", "relative/jiter.so", "/data/jiter.pyd",
                     "/data/jiter.py", "/data/jiter.cpython-313-x86_64-linux-android.so",
                     "/data/jiter.cpython-314-aarch64-linux-android.so", "/data/../jiter.so",
                     "/data//jiter.so", "/data/\x00jiter.so"):
            report = self.report()
            report["packages"][0]["native_modules"][0]["file"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64")

    def test_accept_chaquopy_plain_so_and_abi3_names(self):
        for basename in ("jiter.so", "jiter.abi3.so"):
            report = self.report("jiter")
            report["packages"][0]["native_modules"][0]["file"] = "/data/fixture/" + basename
            verifier.validate(report, "3.14", "x86_64", package_group="jiter")

    def test_reject_wrong_package_module_facts_and_expected_version(self):
        for field, value in (("module", "fake_jiter"), ("native", True), ("native", 0),
                             ("file", "C:\\site-packages\\jiter\\__init__.py"),
                             ("file", "/data/jiter.so")):
            report = self.report("jiter")
            report["packages"][0]["module"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64", package_group="jiter")
        report = self.report("jiter")
        report["packages"][0]["expected_version"] = "0.0.0"
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            verifier.validate(report, "3.14", "x86_64", package_group="jiter")

    def test_reject_bad_report_shapes_and_parameters(self):
        for report in (None, [], "not-json-object", {"schema_version": 1}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                verifier.validate(report, "3.14", "x86_64")
        for options in ({"package_group": "unknown"}, {"expected_api": True}, {"expected_api": 23}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                verifier.validate(self.report(), "3.14", "x86_64", **options)

    def test_cli_package_group_and_exact_api(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(self.report("jiter", api=27)), encoding="utf-8")
            common = [str(path), "--python", "3.14", "--abi", "x86_64", "--package", "jiter"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verifier.main(common + ["--api", "27"]), 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(verifier.main(common + ["--api", "24"]), 1)


if __name__ == "__main__":
    unittest.main()
