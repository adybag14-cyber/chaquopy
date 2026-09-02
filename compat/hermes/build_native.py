#!/usr/bin/env python3
"""Build one locked Android wheel and retain honest build/runtime evidence.

Run on Linux or macOS with the Android SDK and Rust 1.88.0 installed. The Android
cross compiler and CPython testbed are provided by pinned cibuildwheel. ARM64
wheels built on x86_64 are explicitly build-only, not device-certified.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
ABI_IDENTIFIERS = {"arm64-v8a": "arm64_v8a", "x86_64": "x86_64"}
PACKAGES = ("jiter", "pydantic-core")


def build_environment(package: str) -> dict[str, str]:
    # Public sdists/crates do not need the maintainer's ambient credentials or
    # settings which might silently skip tests or change the target platform.
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CIBW_")
        and not name.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY"))
    }
    environment.update(
        {
            "CIBW_BUILD_FRONTEND": "build",
            "CIBW_ENVIRONMENT_ANDROID": (
                "ANDROID_API_LEVEL=24 "
                "MATURIN_PEP517_ARGS=--locked "
                "RUSTFLAGS='-C link-arg=-Wl,-z,max-page-size=16384 "
                "-C link-arg=-Wl,-z,common-page-size=16384'"
            ),
            "CIBW_TEST_COMMAND_ANDROID": (
                "python -m compat.hermes.runtime_smoke "
                f"--package {package} --require-android"
            ),
            "CIBW_TEST_REQUIRES_ANDROID": "typing_extensions==4.15.0",
            "CIBW_TEST_SKIP": "",
            "CIBW_BUILD_VERBOSITY": "1",
            "SOURCE_DATE_EPOCH": "1754006400",
        }
    )
    return environment


def command_for(source: Path, wheel_dir: Path, python: str, abi: str) -> list[str]:
    identifier = f"cp{python.replace('.', '')}-android_{ABI_IDENTIFIERS[abi]}"
    return [
        sys.executable,
        "-m",
        "cibuildwheel",
        str(source),
        "--only",
        identifier,
        "--config-file",
        str(ROOT / "compat/hermes/cibuildwheel.toml"),
        "--output-dir",
        str(wheel_dir),
    ]


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, choices=PACKAGES)
    parser.add_argument("--python", required=True, choices=("3.13", "3.14"))
    parser.add_argument("--abi", required=True, choices=tuple(ABI_IDENTIFIERS))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-cache", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)
    if platform.system() not in {"Linux", "Darwin"}:
        parser.error("Android wheels require Linux/macOS; use WSL or the hosted workflow")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    output = args.output.resolve()
    if output.exists():
        parser.error("output directory already exists; use a new run-specific directory")
    output.mkdir(parents=True)
    wheel_dir = output / "wheels"
    wheel_dir.mkdir()
    report = {
        "schema": "hermes-android-native-build-v1",
        "package": args.package,
        "python": args.python,
        "abi": args.abi,
        "android_api": 24,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "build_status": "failed",
        "runtime_validation": "not_run",
        "host_system": platform.system(),
        "host_machine": platform.machine(),
        "source_lock_sha256": hashlib.sha256(
            (ROOT / "compat/hermes/sources.lock.json").read_bytes()
        ).hexdigest(),
    }
    try:
        driver_version = importlib.metadata.version("cibuildwheel")
        if driver_version != "4.2.0":
            raise ValueError(f"Expected cibuildwheel 4.2.0, got {driver_version}")
        rust_version = subprocess.run(
            ["rustc", "--version"], check=True, capture_output=True,
            text=True, encoding="utf-8", timeout=30,
        ).stdout.strip()
        if not rust_version.startswith("rustc 1.88.0 "):
            raise ValueError(f"Expected Rust 1.88.0, got {rust_version}")
        report["cibuildwheel_version"] = driver_version
        report["rustc_version"] = rust_version
        prepared = subprocess.run(
            [
                sys.executable,
                str(ROOT / "compat/hermes/prepare_sources.py"),
                "prepare", "--package", args.package,
                "--dest", str(args.source_cache.resolve()),
            ],
            cwd=ROOT, check=True, text=True, encoding="utf-8", capture_output=True,
            timeout=180,
        )
        source = json.loads(prepared.stdout)
        source_dir = Path(source["source_dir"])
        shutil.copyfile(source["provenance_path"], output / "source-provenance.json")
        command = command_for(source_dir, wheel_dir, args.python, args.abi)
        report["command"] = command
        subprocess.run(
            command, cwd=ROOT, env=build_environment(args.package),
            check=True, timeout=args.timeout,
        )
        subprocess.run(
            [
                sys.executable, str(ROOT / "compat/hermes/audit_wheels.py"),
                "--wheel-dir", str(wheel_dir), "--python", args.python,
                "--abi", args.abi, "--package-filter", args.package,
                "--output", str(output / "wheel-audit.json"),
            ],
            cwd=ROOT, check=True, timeout=120,
        )
        report["build_status"] = "passed"
        native_machine = "arm64-v8a" if platform.machine() in {"arm64", "aarch64"} else "x86_64"
        report["runtime_validation"] = (
            "cibuildwheel-android-testbed-passed"
            if args.abi == native_machine else "not_run_cross_architecture"
        )
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print(f"Android wheel build rejected: {exc}", file=sys.stderr)
        return 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "build-report.json", report)
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
