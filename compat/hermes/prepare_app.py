#!/usr/bin/env python3
"""Stage the real runtime suite and record an explicit Android-app wheelhouse.

This does not build, install, or claim to certify an APK. All generated files
stay under android/.generated, which is ignored and survives `gradle clean`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve().parent
NATIVE_PACKAGES = {"jiter": "0.16.0", "pydantic_core": "2.46.5"}
ABIS = ("arm64_v8a", "x86_64")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def inspect_wheelhouse(wheelhouse: Path, python_version: str) -> list[dict]:
    if python_version not in ("3.13", "3.14"):
        raise ValueError("Python version must be 3.13 or 3.14")
    if not wheelhouse.is_dir():
        raise ValueError(f"Wheelhouse does not exist: {wheelhouse}")
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise ValueError(
            "Wheelhouse has no directly accessible .whl files (pip find-links is not recursive)"
        )
    tag = "cp" + python_version.replace(".", "")
    for package, version in NATIVE_PACKAGES.items():
        for abi in ABIS:
            expression = re.compile(
                re.escape(f"{package}-{version}-{tag}-")
                + rf"(?:{tag}|abi3)-android_(\d+)_" + re.escape(abi) + r"\.whl"
            )
            matches = [
                path for path in wheels if (match := expression.fullmatch(path.name))
                and int(match[1]) <= 24
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"Expected exactly one {package} {version} Python {python_version} "
                    f"{abi} wheel for Android API <= 24, found {len(matches)}"
                )
    pure_msgpack = wheelhouse / "msgpack-1.2.2-py3-none-any.whl"
    if not pure_msgpack.is_file():
        raise ValueError("Wheelhouse needs the genuinely built msgpack-1.2.2-py3-none-any.whl")
    return [{"filename": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
            for path in wheels]


def prepare_app(
    wheelhouse: Path, python_version: str = "3.14", *,
    android_root: Path = HERE / "android", runtime_source: Path = HERE / "runtime_smoke.py"
) -> dict:
    wheelhouse = wheelhouse.resolve()
    inventory = inspect_wheelhouse(wheelhouse, python_version)
    if not runtime_source.is_file():
        raise ValueError(f"Missing canonical runtime suite: {runtime_source}")
    android_root = android_root.resolve()
    generated = android_root / ".generated"
    python_root = generated / "python"
    if generated.is_symlink() or python_root.is_symlink():
        raise ValueError("Generated source directories must not be symlinks")
    python_root.mkdir(parents=True, exist_ok=True)
    if any(path.name != "runtime_smoke.py" for path in python_root.iterdir()):
        raise ValueError(
            "Unexpected generated Python sources; refusing to package unrelated modules"
        )
    output = python_root / "runtime_smoke.py"
    if output.is_symlink():
        raise ValueError("Generated runtime module must not be a symlink")
    source_bytes = runtime_source.read_bytes()
    output.write_bytes(source_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    for name, content in (
            ("runtime_smoke.sha256", source_hash), ("python-version.txt", python_version),
            ("wheelhouse.txt", str(wheelhouse))):
        path = generated / name
        if path.is_symlink():
            raise ValueError(f"Generated metadata must not be a symlink: {path}")
        path.write_text(content + "\n", encoding="utf-8")
    receipt = {
        "schema_version": 1,
        "python_version": python_version,
        "android_root": str(android_root),
        "wheelhouse": str(wheelhouse),
        "wheels": inventory,
        "runtime_smoke_sha256": source_hash,
        "generated_python_source": str(output),
        "application_id": "com.mobilefork.chaquopycompat",
        "android_api": 24,
        "abis": ["arm64-v8a", "x86_64"],
        "android_compatibility_verified": False,
    }
    receipt_path = generated / "preparation.json"
    if receipt_path.is_symlink():
        raise ValueError("Generated preparation receipt must not be a symlink")
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--python-version", choices=("3.13", "3.14"), default="3.14")
    args = parser.parse_args(argv)
    try:
        receipt = prepare_app(args.wheelhouse, args.python_version)
    except (OSError, ValueError) as error:
        print(f"App preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
