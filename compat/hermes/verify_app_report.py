#!/usr/bin/env python3
"""Reject host, wrong-Python, skipped, or failed standalone Android app evidence."""

import argparse
import json
from pathlib import Path


def validate(report, python, abi):
    platform = report.get("platform", {})
    tests = report.get("tests", {})
    expected_machine = {"arm64-v8a": {"aarch64", "arm64"}, "x86_64": {"x86_64"}}[abi]
    conditions = {
        "successful full suite": report.get("status") == "passed"
        and report.get("package_group") == "full",
        "actual Android execution": report.get("android_execution_verified") is True
        and platform.get("is_android") is True and platform.get("platform") == "android",
        "selected Python": str(platform.get("python", "")).startswith(python + "."),
        "selected ABI": platform.get("machine") in expected_machine,
        "minimum Android API": type(platform.get("android_api_level")) is int
        and platform["android_api_level"] >= 24,
        "measured page size": platform.get("page_size") in {4096, 16384, 65536},
        "nonempty tests": type(tests.get("run")) is int and tests["run"] > 0,
        "no failed or skipped tests": tests.get("failures") == []
        and tests.get("errors") == [] and tests.get("skipped") == [],
        "complete passed-test list": isinstance(tests.get("passed"), list)
        and len(tests["passed"]) == tests.get("run"),
        "bounded fixture scope": report.get("network_scope")
        == "loopback-and-in-memory-fixtures-only"
        and report.get("service_inference_verified") is False,
    }
    failed = [name for name, valid in conditions.items() if not valid]
    if failed:
        raise ValueError("Invalid Android app evidence: " + ", ".join(failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--python", required=True, choices=("3.13", "3.14"))
    parser.add_argument("--abi", required=True, choices=("arm64-v8a", "x86_64"))
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    validate(report, args.python, args.abi)
    print(f"Android app compatibility passed: Python {args.python}, {args.abi}, "
          f"{report['tests']['run']} tests, page size {report['platform']['page_size']}")


if __name__ == "__main__":
    main()
