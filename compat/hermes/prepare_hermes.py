#!/usr/bin/env python3
"""Prepare a nonproduction Hermes consumer of the verified Chaquopy experiment.

Unlike the dependency-only test app, this retains Hermes' complementary Python
requirements and adds the genuine SDK set. Android wheels are resolved with pip
from PyPI/Chaquopy, never retagged host wheels. The output is an offline input
bundle with a closed hash inventory; creating it is not runtime certification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

try:
    from . import prepare_bootstrap
except ImportError:
    import prepare_bootstrap

HERE = Path(__file__).resolve().parent
SCHEMA = "hermes-chaquopy-consumer-v1"
EXTRAS = """
rich==14.3.3
markdown-it-py==4.0.0
mdurl==0.1.2
pygments==2.20.0
jinja2==3.1.6
MarkupSafe==3.0.3
packaging==26.3
filelock==3.24.3
platformdirs==4.9.4
urllib3==2.7.0
chaquopy-libyaml==0.2.5
colorama==0.4.6
"""


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_pins(text):
    pins = {}
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9_.!+]+)", value)
        if not match:
            raise ValueError("Every consumer requirement must be an exact pin")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in pins:
            raise ValueError(f"Duplicate requirement: {name}")
        pins[name] = match[2]
    return pins


def consumer_requirements(hermes_text, sdk_text, extras_text=EXTRAS):
    pins = parse_pins(hermes_text)
    pins.update(parse_pins(sdk_text))
    pins.update(parse_pins(extras_text))
    return "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))


def complementary_requirements(consumer_text, sdk_text):
    consumer = parse_pins(consumer_text)
    sdk = parse_pins(sdk_text)
    if any(consumer.get(name) != version for name, version in sdk.items()):
        raise ValueError("Consumer must preserve the verified SDK package versions")
    return "".join(f"{name}=={version}\n" for name, version in sorted(consumer.items()) if name not in sdk)


def validate_inventory(root, inventory):
    root = Path(root).resolve()
    expected = set()
    for item in inventory:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or str(relative) in expected:
            raise ValueError("Invalid or duplicate consumer artifact path")
        expected.add(str(relative))
        path = root / relative
        if (not path.resolve().is_relative_to(root) or path.is_symlink()
                or not path.is_file() or path.stat().st_size != item["bytes"]
                or sha256(path) != item["sha256"]):
            raise ValueError(f"Changed consumer artifact: {relative}")
    actual = {str(path.relative_to(root)) for path in root.rglob("*")
              if path.is_file() and path != root / "consumer.json"}
    if actual != expected:
        raise ValueError("Consumer artifact inventory has unrecorded or missing files")


def verify(root):
    root = Path(root).resolve()
    receipt = json.loads((root / "consumer.json").read_text(encoding="utf-8"))
    if receipt.get("schema") != SCHEMA or receipt.get("python") != "3.13":
        raise ValueError("Unsupported Hermes experiment receipt")
    validate_inventory(root, receipt["files"])
    return receipt


def prepare(hermes_root, wheelhouse, bootstrap, output):
    if sys.version_info[:2] != (3, 13):
        raise ValueError("The first Hermes integration lane requires build Python 3.13")
    output = output.resolve()
    if output.exists():
        raise ValueError("Use a new output directory")
    bootstrap = bootstrap.resolve()
    verified_bootstrap = prepare_bootstrap.validate_receipt(
        bootstrap / "receipt.json", expected_python="3.13")
    sdk_manifest = json.loads((wheelhouse / "wheelhouse-manifest.json").read_text())
    for item in sdk_manifest["wheels"]:
        filename = item.get("filename", item.get("file"))
        if not filename or Path(filename).name != filename:
            raise ValueError("SDK manifest wheel has an invalid filename")
        path = wheelhouse / filename
        if sha256(path) != item["sha256"]:
            raise ValueError("SDK wheel changed after assembly")
    sdk_requirements = (HERE / "android/requirements.txt").read_text()
    requirements = consumer_requirements(
        (hermes_root / "requirements-android-chaquopy.txt").read_text(),
        sdk_requirements)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hermes-consumer-", dir=output.parent) as temporary:
        stage = Path(temporary) / "consumer"
        stage.mkdir()
        requirements_path = stage / "requirements.txt"
        requirements_path.write_text(requirements, encoding="utf-8")
        wheels = stage / "wheels"
        wheels.mkdir()
        # --find-links is not an authority: pip may prefer a different equal-version
        # upstream wheel. Preserve every already-verified SDK wheel byte-for-byte,
        # and permit the resolver to fetch only the complementary Hermes packages.
        for item in sdk_manifest["wheels"]:
            filename = item.get("filename", item.get("file"))
            shutil.copyfile(wheelhouse / filename, wheels / filename)
        complement = Path(temporary) / "complement.txt"
        complement.write_text(complementary_requirements(requirements, sdk_requirements), encoding="utf-8")
        for abi in ("arm64_v8a", "x86_64"):
            subprocess.run(
                [sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check",
                 "--no-deps", "--only-binary=:all:", "--platform", "android_24_" + abi,
                 "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
                 "--index-url", "https://pypi.org/simple",
                 "--extra-index-url", "https://chaquo.com/pypi-13.1",
                 "--dest", str(wheels), "-r", str(complement)], check=True,
            )
            # Prove dependency closure against the final offline bytes, without
            # allowing network substitution or installing Android code on the host.
            subprocess.run(
                [sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check",
                 "--no-index", "--only-binary=:all:", "--platform", "android_24_" + abi,
                 "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
                 "--find-links", str(wheels), "--dest", str(Path(temporary) / ("closure-" + abi)),
                 "-r", str(requirements_path)], check=True,
            )
        shutil.copytree(bootstrap / "maven", stage / "maven")
        shutil.copyfile(bootstrap / "receipt.json", stage / "bootstrap-receipt.json")
        python_sources = stage / "python"
        python_sources.mkdir()
        shutil.copyfile(HERE / "runtime_smoke.py", python_sources / "runtime_smoke.py")
        shutil.copyfile(HERE / "hermes_runtime_smoke.py", python_sources / "hermes_runtime_smoke.py")
        files = [{"path": path.relative_to(stage).as_posix(), "bytes": path.stat().st_size,
                  "sha256": sha256(path)} for path in sorted(stage.rglob("*")) if path.is_file()]
        receipt = {
            "schema": SCHEMA, "python": "3.13", "bootstrap_version": verified_bootstrap["version"],
            "bootstrap_sha256": verified_bootstrap["archive_sha256"],
            "source_lock_sha256": sha256(HERE / "sources.lock.json"),
            "hermes_requirements_sha256": sha256(hermes_root / "requirements-android-chaquopy.txt"),
            "sdk_requirements_sha256": sha256(HERE / "android/requirements.txt"),
            "files": files, "runtime_tested": False,
        }
        (stage / "consumer.json").write_text(json.dumps(receipt, indent=2) + "\n")
        validate_inventory(stage, files)
        stage.rename(output)
    return verify(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    for option in ("hermes-root", "wheelhouse", "bootstrap", "output"):
        build.add_argument("--" + option, type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        receipt = prepare(args.hermes_root, args.wheelhouse, args.bootstrap, args.output)
    else:
        receipt = verify(args.root)
    print(json.dumps({"python": receipt["python"], "bootstrap_sha256": receipt["bootstrap_sha256"],
                      "files": len(receipt["files"]), "runtime_tested": False}))


if __name__ == "__main__":
    main()
