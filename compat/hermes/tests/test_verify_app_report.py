import copy
import unittest

from compat.hermes.verify_app_report import validate


class AppReportTests(unittest.TestCase):
    def report(self):
        return {
            "status": "passed", "package_group": "full", "android_execution_verified": True,
            "platform": {"platform": "android", "is_android": True, "python": "3.14.0",
                         "machine": "x86_64", "android_api_level": 35, "page_size": 16384},
            "tests": {"run": 1, "passed": ["test"], "failures": [], "errors": [], "skipped": []},
            "network_scope": "loopback-and-in-memory-fixtures-only",
            "service_inference_verified": False,
        }

    def test_accept_actual_selected_android_lane(self):
        validate(self.report(), "3.14", "x86_64")

    def test_reject_host_wrong_target_and_skips(self):
        for section, key, value in (
            ("platform", "platform", "linux"), ("platform", "machine", "aarch64"),
            ("platform", "python", "3.13.9"), ("platform", "page_size", None),
            ("tests", "run", 0), ("tests", "skipped", ["missing wheel"]),
        ):
            report = copy.deepcopy(self.report())
            report[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(report, "3.14", "x86_64")


if __name__ == "__main__":
    unittest.main()
