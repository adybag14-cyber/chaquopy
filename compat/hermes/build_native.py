#!/usr/bin/env python3
"""Build one locked Android wheel and retain honest build/runtime evidence.

Run on Linux or macOS with the Android SDK and Rust 1.88.0 installed. The Android
cross compiler and CPython testbed are provided by pinned cibuildwheel. ARM64
wheels built on x86_64 are explicitly build-only, not device-certified.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time

try:
    from .verify_app_report import validate as validate_runtime_report
except ImportError:
    from verify_app_report import validate as validate_runtime_report


ROOT = Path(__file__).resolve().parents[2]
ABI_IDENTIFIERS = {"arm64-v8a": "arm64_v8a", "x86_64": "x86_64"}
PACKAGES = ("jiter", "pydantic-core")


def build_environment(package: str, *, build_only: bool = False,
                      source_dir: Path | None = None) -> dict[str, str]:
    # Public sdists/crates do not need the maintainer's ambient credentials or
    # settings which might silently skip tests or change the target platform.
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CIBW_")
        and not name.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY"))
    }
    rust_flags = (
        "-C link-arg=-Wl,-z,max-page-size=16384 "
        "-C link-arg=-Wl,-z,common-page-size=16384"
    )
    if source_dir is not None:
        # Rust embeds source paths in panic sites even in stripped release ELF.
        # Normalize both package and Cargo registry paths across build roots.
        mappings = [(Path.home(), "/hermes-build-home"),
                    (Path(environment.get("CARGO_HOME", Path.home() / ".cargo")), "/hermes-cargo"),
                    (source_dir.resolve(), "/hermes-source")]
        for source, target in mappings:
            if any(character.isspace() for character in str(source)):
                raise ValueError("Android release build paths must not contain whitespace")
            rust_flags += f" --remap-path-prefix={source}={target}"
    environment.update(
        {
            "CIBW_BUILD_FRONTEND": "build",
            "CIBW_ENVIRONMENT_ANDROID": (
                "ANDROID_API_LEVEL=24 "
                "MATURIN_PEP517_ARGS=--locked "
                f"RUSTFLAGS={shlex.quote(rust_flags)}"
            ),
            "CIBW_TEST_COMMAND_ANDROID": (
                "python -m compat.hermes.runtime_smoke "
                f"--package {package} --require-android --log-marker"
            ),
            "CIBW_TEST_REQUIRES_ANDROID": "typing_extensions==4.15.0",
            "CIBW_TEST_SKIP": "*" if build_only else "",
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


def run_logged(command, environment, timeout, log_path):
    """Stream logs and capture the actual Android report, with an owned timeout group."""
    reports = []
    lines = queue.Queue()
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        start_new_session=True,
    )

    def read_lines():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    deadline = time.monotonic() + timeout
    completed = False
    try:
        reader.start()
        with log_path.open("w", encoding="utf-8") as log:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    line = lines.get(timeout=min(1, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    break
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
                marker = "HERMES_RUNTIME_SMOKE_JSON="
                if marker in line:
                    reports.append(json.loads(line.split(marker, 1)[1]))
        code = process.wait(timeout=max(1, deadline - time.monotonic()))
        if code:
            raise subprocess.CalledProcessError(code, command)
        completed = True
        return reports
    finally:
        if not completed:
            # This is the new session created immediately above, never the
            # caller's shell/Codex process group or an unrelated emulator.
            # The leader may already have exited while a child holds stdout.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if reader.ident is not None:
            reader.join(timeout=5)
        # Avoid blocking on TextIOWrapper's lock if a separately sessioned
        # descendant retained the pipe. The reader owns it until EOF.
        if not reader.is_alive():
            process.stdout.close()


def record_runtime_evidence(report, runtime_reports, output, *, python, abi,
                            package, host_machine, build_only=False):
    if build_only:
        if runtime_reports:
            raise ValueError("Unexpected runtime report for an explicit build-only invocation")
        report["runtime_validation"] = "not_run_explicit_build_only"
        return
    if host_machine in {"arm64", "aarch64"}:
        native_abi = "arm64-v8a"
    elif host_machine in {"x86_64", "AMD64"}:
        native_abi = "x86_64"
    else:
        raise ValueError(f"Unsupported Android testbed host: {host_machine}")
    if abi != native_abi:
        if runtime_reports:
            raise ValueError("Unexpected runtime report for a cross-architecture build")
        report["runtime_validation"] = "not_run_cross_architecture"
        return
    if len(runtime_reports) != 1:
        raise ValueError("Expected exactly one measured Android runtime report")
    runtime_report = runtime_reports[0]
    validate_runtime_report(runtime_report, python, abi, package_group=package)
    runtime_path = output / "runtime-report.json"
    write_json(runtime_path, runtime_report)
    report["runtime_report_sha256"] = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    report["runtime_android_api"] = runtime_report["platform"]["android_api_level"]
    report["runtime_page_size"] = runtime_report["platform"]["page_size"]
    report["runtime_python"] = runtime_report["platform"]["python"]
    report["runtime_validation"] = "cibuildwheel-android-testbed-passed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, choices=PACKAGES)
    parser.add_argument("--python", required=True, choices=("3.13", "3.14"))
    parser.add_argument("--abi", required=True, choices=tuple(ABI_IDENTIFIERS))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-cache", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--build-only", action="store_true", help=(
        "Compile/audit without launching a testbed, for downstream source builds. "
        "Never produces runtime certification; the app/device matrix remains required."))
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
        "build_only": args.build_only,
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
        runtime_reports = run_logged(
            command, build_environment(args.package, build_only=args.build_only,
                                       source_dir=source_dir), args.timeout,
            output / "cibuildwheel.log",
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
        record_runtime_evidence(
            report, runtime_reports, output, python=args.python, abi=args.abi,
            package=args.package, host_machine=platform.machine(),
            build_only=args.build_only,
        )
        report["build_status"] = "passed"
        return 0
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError,
            subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print(f"Android wheel build rejected: {exc}", file=sys.stderr)
        return 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "build-report.json", report)
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
