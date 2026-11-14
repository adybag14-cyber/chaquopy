#!/usr/bin/env python3
"""Stage the runtime suite and bind an explicit wheelhouse and patched bootstrap.

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
NATIVE_PACKAGES = {"jiter": "0.19.0", "pydantic_core": "2.50.4"}
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
                + rf"(?:{tag}|abi3)-android_(\d+)_"
                + re.escape(abi)
                + r"\.whl"
            )
            matches = [
                path
                for path in wheels
                if (match := expression.fullmatch(path.name)) and int(match[1]) <= 24
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"Expected exactly one {package} {version} Python {python_version} "
                    f"{abi} wheel for Android API <= 24, found {len(matches)}"
                )
    pure_msgpack = wheelhouse / "msgpack-1.2.4-py3-none-any.whl"
    if not pure_msgpack.is_file():
        raise ValueError("Wheelhouse needs the genuinely built msgpack-1.2.4-py3-none-any.whl")
    return [
        {"filename": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
        for path in wheels
    ]


def validate_bootstrap_receipt(path: Path, *, expected_python: str) -> dict:
    """Delegate source/archive/bytecode validation to the canonical helper."""
    try:
        from .prepare_bootstrap import validate_receipt
    except ImportError:  # Direct script execution; never a validation fallback.
        from prepare_bootstrap import validate_receipt
    return validate_receipt(path, expected_python=expected_python)


def inspect_bootstrap(bootstrap: Path, python_version: str) -> dict:
    if not bootstrap.is_dir() or bootstrap.is_symlink():
        raise ValueError(f"Patched bootstrap must be an existing real directory: {bootstrap}")
    bootstrap = bootstrap.resolve()
    receipt_path = bootstrap / "receipt.json"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ValueError(f"Missing or linked patched bootstrap receipt: {receipt_path}")
    if receipt_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Patched bootstrap receipt exceeds the byte limit")
    original_bytes = receipt_path.read_bytes()
    verified = validate_bootstrap_receipt(receipt_path, expected_python=python_version)
    if not isinstance(verified, dict):
        raise ValueError("Bootstrap validator did not return a verified receipt")
    if receipt_path.read_bytes() != original_bytes:
        raise ValueError("Bootstrap receipt changed during validation")
    expected_fields = (
        "schema",
        "python",
        "version",
        "archive_path",
        "archive_sha256",
        "archive_bytes",
        "importer_source_sha256",
        "importer_code_sha256",
        "source_lock_sha256",
        "input_sha256",
        "source_inventory_sha256",
        "pyc_magic_hex",
        "maven_repo_path",
        "receipt_path",
    )
    if any(key not in verified for key in expected_fields):
        raise ValueError("Bootstrap validator returned incomplete provenance")
    if verified["python"] != python_version:
        raise ValueError("Verified bootstrap Python differs from app Python")
    if (
        Path(verified["receipt_path"]).resolve() != receipt_path
        or Path(verified["maven_repo_path"]).resolve() != bootstrap / "maven"
    ):
        raise ValueError("Verified bootstrap receipt/Maven paths differ from selected directory")
    raw_receipt = json.loads(original_bytes)
    if {
        key: value
        for key, value in verified.items()
        if key not in ("receipt_path", "maven_repo_path")
    } != raw_receipt:
        raise ValueError("Verified bootstrap provenance differs from receipt bytes")
    if not isinstance(verified["version"], str) or not re.fullmatch(
        r"[0-9]+[A-Za-z0-9_.+-]*", verified["version"]
    ):
        raise ValueError("Invalid verified bootstrap version")
    if not isinstance(verified["archive_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", verified["archive_sha256"]
    ):
        raise ValueError("Invalid verified bootstrap archive SHA-256")
    return {
        "directory": str(bootstrap),
        "receipt_sha256": hashlib.sha256(original_bytes).hexdigest(),
        **{key: verified[key] for key in expected_fields},
    }


def prepare_app(
    wheelhouse: Path,
    python_version: str = "3.14",
    *,
    bootstrap: Path,
    android_root: Path = HERE / "android",
    runtime_source: Path = HERE / "runtime_smoke.py",
) -> dict:
    wheelhouse = wheelhouse.resolve()
    inventory = inspect_wheelhouse(wheelhouse, python_version)
    bootstrap_provenance = inspect_bootstrap(bootstrap, python_version)
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
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    stamps = (
        ("runtime_smoke.sha256", source_hash),
        ("python-version.txt", python_version),
        ("wheelhouse.txt", str(wheelhouse)),
        ("bootstrap-path.txt", bootstrap_provenance["directory"]),
        ("bootstrap-version.txt", bootstrap_provenance["version"]),
        ("bootstrap-receipt.sha256", bootstrap_provenance["receipt_sha256"]),
        ("bootstrap-archive.sha256", bootstrap_provenance["archive_sha256"]),
    )
    for name, _ in stamps:
        path = generated / name
        if path.is_symlink():
            raise ValueError(f"Generated metadata must not be a symlink: {path}")
    receipt = {
        "schema_version": 1,
        "python_version": python_version,
        "android_root": str(android_root),
        "wheelhouse": str(wheelhouse),
        "wheels": inventory,
        "bootstrap_provenance": bootstrap_provenance,
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
    output.write_bytes(source_bytes)
    for name, content in stamps:
        (generated / name).write_text(content + "\n", encoding="utf-8")
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--python-version", choices=("3.13", "3.14"), default="3.14")
    args = parser.parse_args(argv)
    try:
        receipt = prepare_app(args.wheelhouse, args.python_version, bootstrap=args.bootstrap)
    except (ImportError, OSError, ValueError) as error:
        print(f"App preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
