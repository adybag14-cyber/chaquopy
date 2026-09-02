#!/usr/bin/env python3
"""Verify complete Android runtime receipts without importing tested dependencies.

Expected test identities and package pins come from runtime_smoke's declarative
suite. No test method or third-party package is executed by this verifier. Module
paths are checked as recorded Android facts, not opened on the verification host.
This checks evidence consistency; JSON is not a cryptographic device attestation.
"""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys

try:
    from . import runtime_smoke
except ImportError:  # Direct script execution.
    import runtime_smoke


MACHINES = {"arm64-v8a": {"aarch64", "arm64"}, "x86_64": {"x86_64"}}
TRIPLETS = {"arm64-v8a": "aarch64-linux-android", "x86_64": "x86_64-linux-android"}
CASE_ID = re.compile(r"(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*\Z")


def require(condition, message):
    if not condition:
        raise ValueError("Invalid Android runtime evidence: " + message)


def normalized_case_id(value):
    require(isinstance(value, str) and CASE_ID.fullmatch(value), "malformed passed-test identity")
    return ".".join(value.rsplit(".", 2)[-2:])


def expected_cases(package_group):
    """Construct unittest cases for identity inspection only; do not run them."""
    require(isinstance(package_group, str) and package_group in runtime_smoke.PACKAGE_GROUPS,
            "unknown package group")
    return {normalized_case_id(case.id()) for case in runtime_smoke.build_suite(package_group)}


def validate_module(record, name, *, native, soabi):
    require(isinstance(record, dict), f"missing module evidence for {name}")
    require(record.get("module") == name and record.get("native") is native,
            f"module/native identity mismatch for {name}")
    filename = record.get("file")
    require(isinstance(filename, str) and filename.startswith("/") and not filename.startswith("//")
            and "\\" not in filename and not any(ord(char) < 32 for char in filename),
            f"module file must be an absolute Android path for {name}")
    path = PurePosixPath(filename)
    require(".." not in path.parts and str(path) == filename,
            f"module file has noncanonical path for {name}")
    if native:
        leaf = name.rsplit(".", 1)[-1]
        # Chaquopy supports plain .so and abi3 names as well as tagged CPython
        # extension names. When tagged, the tag must match this Python and ABI.
        require(path.name in {f"{leaf}.so", f"{leaf}.abi3.so", f"{leaf}.{soabi}.so"},
                f"native extension file/target mismatch for {name}")
    else:
        require(filename.endswith((".py", ".pyc")), f"package module is not Python code for {name}")


def validate_packages(records, package_group, soabi):
    require(isinstance(records, list), "package evidence must be a list")
    expected = runtime_smoke.PACKAGE_GROUPS[package_group]
    require(len(records) == len(expected) and all(isinstance(item, dict) for item in records),
            "package evidence count or shape mismatch")
    names = [item.get("distribution") for item in records]
    require(all(isinstance(name, str) for name in names) and len(set(names)) == len(names)
            and set(names) == set(expected), "missing, duplicate, or unexpected package evidence")
    for record in records:
        name = record["distribution"]
        version, module_name, native_names = runtime_smoke.PACKAGE_SPECS[name]
        require(record.get("version") == record.get("expected_version") == version,
                f"pinned version mismatch for {name}")
        validate_module(record.get("module"), module_name, native=False, soabi=soabi)
        native_modules = record.get("native_modules")
        require(isinstance(native_modules, list) and len(native_modules) == len(native_names)
                and all(isinstance(item, dict) for item in native_modules),
                f"native module evidence count or shape mismatch for {name}")
        actual_names = [item.get("module") for item in native_modules]
        require(all(isinstance(item, str) for item in actual_names)
                and len(set(actual_names)) == len(actual_names)
                and set(actual_names) == set(native_names),
                f"missing, duplicate, or unexpected native module for {name}")
        for module in native_modules:
            validate_module(module, module["module"], native=True, soabi=soabi)


def validate(report, python, abi, *, package_group="full", expected_api=None):
    require(python in ("3.13", "3.14"), "unsupported Python target")
    require(isinstance(abi, str) and abi in MACHINES, "unsupported ABI target")
    cases = expected_cases(package_group)
    require(expected_api is None or type(expected_api) is int and expected_api >= 24,
            "expected API must be an integer >=24")
    require(isinstance(report, dict), "report must be an object")
    require(type(report.get("schema_version")) is int and report["schema_version"] == 1,
            "unsupported evidence schema")
    require(report.get("status") == "passed" and report.get("package_group") == package_group
            and "preflight_error" not in report, "selected runtime suite did not pass")
    require(report.get("require_android") is True
            and report.get("android_execution_verified") is True,
            "Android-required execution was not verified")

    platform = report.get("platform")
    require(isinstance(platform, dict), "missing platform evidence")
    require(platform.get("implementation") == "CPython" and platform.get("platform") == "android"
            and platform.get("is_android") is True, "not actual Android CPython execution")
    actual_python = platform.get("python")
    require(isinstance(actual_python, str)
            and re.fullmatch(re.escape(python) + r"\.\d+", actual_python),
            "selected Python mismatch")
    require(isinstance(platform.get("machine"), str) and platform["machine"] in MACHINES[abi],
            "selected ABI mismatch")
    soabi = f"cpython-{python.replace('.', '')}-{TRIPLETS[abi]}"
    require(platform.get("soabi") == soabi, "selected Python/Android SOABI mismatch")
    api = platform.get("android_api_level")
    require(type(api) is int and api >= 24, "unsupported or missing Android API level")
    require(expected_api is None or api == expected_api, "exact Android API mismatch")
    require(type(platform.get("page_size")) is int
            and platform["page_size"] in {4096, 16384, 65536},
            "missing or unsupported measured page size")

    tests = report.get("tests")
    require(isinstance(tests, dict), "missing test evidence")
    require(type(tests.get("run")) is int and tests["run"] == len(cases),
            "exact required test count mismatch")
    require(tests.get("failures") == [] and tests.get("errors") == []
            and tests.get("skipped") == [],
            "failed or skipped tests")
    passed = tests.get("passed")
    require(isinstance(passed, list) and len(passed) == len(cases), "passed-test count mismatch")
    normalized = [normalized_case_id(case) for case in passed]
    require(len(set(normalized)) == len(normalized) and set(normalized) == cases,
            "missing, duplicate, or unexpected passed-test identities")
    validate_packages(report.get("packages"), package_group, soabi)
    require(report.get("network_scope") == "loopback-and-in-memory-fixtures-only"
            and report.get("service_inference_verified") is False, "unbounded fixture scope")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--python", required=True, choices=("3.13", "3.14"))
    parser.add_argument("--abi", required=True, choices=tuple(MACHINES))
    parser.add_argument("--package", choices=tuple(runtime_smoke.PACKAGE_GROUPS), default="full")
    parser.add_argument("--api", type=int, help="Require this exact observed Android API level")
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        validate(report, args.python, args.abi, package_group=args.package, expected_api=args.api)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Android {args.package} compatibility passed: Python {args.python}, {args.abi}, "
          f"API {report['platform']['android_api_level']}, {report['tests']['run']} tests, "
          f"page size {report['platform']['page_size']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
