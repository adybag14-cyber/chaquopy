#!/usr/bin/env python3
"""Acquire hash-locked upstream sdists without executing package code.

Requires Python 3.11+ (tomllib). The prepare command prints a JSON receipt to
stdout; diagnostics go to stderr. MSGPACK_PUREPYTHON must be set by the caller
when building msgpack, as recorded in build_environment. No wheel is built or
retagged here, and source preparation alone does not prove Android support.
"""

from __future__ import annotations

import argparse
import copy
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_LOCK = Path(__file__).with_name("sources.lock.json")
PROVENANCE_FILENAME = ".hermes-source-provenance.json"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 10_000
CHUNK_BYTES = 1024 * 1024


class PreparationError(ValueError):
    """The locked input or the untrusted archive failed validation."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_parts(name: str) -> tuple[str, ...]:
    # Backslashes, drive/ADS colons and Windows-normalized names are forbidden
    # even when the builder is Linux, so the same archive is safe on both hosts.
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise PreparationError(f"Unsafe archive path: {name!r}")
    parts = PurePosixPath(name).parts
    if not parts or any(
        part in (".", "..") or ":" in part or part.endswith((".", " "))
        for part in parts
    ):
        raise PreparationError(f"Unsafe archive path: {name!r}")
    # PurePath normalizes explicit './' and empty components: reject rather
    # than allowing aliases which could evade duplicate-member checks.
    if "/".join(parts) != name.rstrip("/"):
        raise PreparationError(f"Non-canonical archive path: {name!r}")
    return parts


def _require_pypi_download_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "files.pythonhosted.org"
        or not parsed.path.startswith("/packages/")
        or parsed.query
        or parsed.fragment
    ):
        raise PreparationError(f"Not an official HTTPS PyPI file URL: {url!r}")


def load_lock(path: Path = DEFAULT_LOCK) -> dict[str, Any]:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1 or not isinstance(lock.get("packages"), dict):
        raise PreparationError("Unsupported or malformed source lock")
    for name, entry in lock["packages"].items():
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise PreparationError(f"Invalid package name: {name!r}")
        if len(_safe_parts(entry["source_directory"])) != 1:
            raise PreparationError("Source directory must be one safe path component")
        if len(_safe_parts(entry["filename"])) != 1:
            raise PreparationError("Archive filename must be one safe path component")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise PreparationError(f"Invalid SHA-256 for {name}")
        if type(entry["bytes"]) is not int or not 0 < entry["bytes"] <= MAX_ARCHIVE_BYTES:
            raise PreparationError(f"Invalid archive byte limit for {name}")
        _require_pypi_download_url(entry["url"])
    return lock


def download_archive(entry: dict[str, Any], target: Path, timeout: float) -> None:
    if not 0 < timeout <= 300:
        raise PreparationError("Download timeout must be in (0, 300] seconds")
    _require_pypi_download_url(entry["url"])
    request = Request(entry["url"], headers={"User-Agent": "hermes-chaquopy-source-preparer/1"})
    with urlopen(request, timeout=timeout) as response, target.open("xb") as output:
        _require_pypi_download_url(response.geturl())
        total = 0
        while chunk := response.read(CHUNK_BYTES):
            total += len(chunk)
            if total > entry["bytes"]:
                raise PreparationError("Download exceeds the locked archive byte count")
            output.write(chunk)


def verify_archive(archive: Path, entry: dict[str, Any]) -> None:
    if archive.stat().st_size != entry["bytes"]:
        raise PreparationError("Archive size does not match the source lock")
    if sha256_file(archive) != entry["sha256"]:
        raise PreparationError("Archive SHA-256 does not match the source lock")


def extract_archive(archive: Path, destination: Path, expected_root: str) -> Path:
    """Extract only regular files/directories after validating the whole index."""
    with tarfile.open(archive, mode="r:gz") as source:
        members = []
        seen: set[str] = set()
        total = 0
        for member in source:
            if len(members) >= MAX_MEMBERS:
                raise PreparationError("Archive contains too many members")
            parts = _safe_parts(member.name)
            if parts[0] != expected_root:
                raise PreparationError(f"Unexpected archive root: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise PreparationError(
                    f"Archive links/special files are forbidden: {member.name!r}"
                )
            canonical = "/".join(parts)
            # Case-fold because this may be prepared on Windows.
            if canonical.casefold() in seen:
                raise PreparationError(f"Duplicate archive member: {member.name!r}")
            seen.add(canonical.casefold())
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise PreparationError(
                    f"Archive member exceeds its byte limit: {member.name!r}"
                )
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise PreparationError("Archive exceeds its expanded byte limit")
            members.append((member, parts))
        for member, parts in members:
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as input_stream, target.open("xb") as output:
                    shutil.copyfileobj(input_stream, output, length=CHUNK_BYTES)
                if target.stat().st_size != member.size:
                    raise PreparationError(f"Truncated archive member: {member.name!r}")
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    root = destination / expected_root
    if not root.is_dir():
        raise PreparationError("Archive did not produce the expected source directory")
    return root


def verify_identity(source: Path, name: str, version: str) -> None:
    metadata = Parser().parsestr((source / "PKG-INFO").read_text(encoding="utf-8"))
    if (normalize_package(metadata.get("Name", "")) != normalize_package(name)
            or metadata.get("Version") != version):
        raise PreparationError("The sdist PKG-INFO identity differs from the source lock")


def normalize_package(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def prepare_build_requirements(source: Path, entry: dict[str, Any]) -> list[dict[str, str]]:
    path = source / "pyproject.toml"
    original_bytes = path.read_bytes()
    original_text = original_bytes.decode("utf-8")
    parsed = tomllib.loads(original_text)
    build = entry["build"]
    if parsed.get("build-system", {}).get("build-backend") != build["backend"]:
        raise PreparationError("The upstream build backend differs from the source lock")
    if build["backend"] != "maturin":
        return []
    old = build["original_requirement"]
    new = build["pinned_requirement"]
    if parsed["build-system"]["requires"] != [old]:
        raise PreparationError("The upstream maturin requirement differs from the source lock")
    section = re.search(r"(?ms)^\[build-system\][^\n]*\n.*?(?=^\[|\Z)", original_text)
    if section is None:
        raise PreparationError("Cannot locate the upstream build-system section")
    replacement, count = re.subn(
        r"(['\"])" + re.escape(old) + r"\1",
        lambda match: match[1] + new + match[1],
        section[0],
    )
    if count != 1:
        raise PreparationError("Cannot unambiguously pin the upstream maturin requirement")
    new_text = original_text[:section.start()] + replacement + original_text[section.end():]
    expected = copy.deepcopy(parsed)
    expected["build-system"]["requires"] = [new]
    if tomllib.loads(new_text) != expected:
        raise PreparationError("Unexpected TOML change while pinning maturin")
    new_bytes = new_text.encode("utf-8")
    path.write_bytes(new_bytes)
    return [{
        "path": "pyproject.toml",
        "kind": "pin-build-requirement",
        "from": old,
        "to": new,
        "before_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "after_sha256": hashlib.sha256(new_bytes).hexdigest(),
    }]


def prepare_package(
    package: str,
    dest: Path,
    *,
    lock_path: Path = DEFAULT_LOCK,
    archive_path: Path | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    """Return a receipt; fail rather than overwrite any existing source tree."""
    lock = load_lock(lock_path)
    package = normalize_package(package)
    if package not in lock["packages"]:
        raise PreparationError(f"Package {package!r} is not in the source lock")
    entry = lock["packages"][package]
    dest = dest.resolve()
    final_source = dest / entry["source_directory"]
    if final_source.exists() or final_source.is_symlink():
        raise PreparationError(f"Refusing to overwrite existing source: {final_source}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hermes-source-", dir=dest) as temporary:
        stage = Path(temporary)
        archive = stage / entry["filename"]
        if archive_path is None:
            download_archive(entry, archive, timeout)
        else:
            # Verify the private staged copy, not a mutable caller-owned path.
            if archive_path.stat().st_size != entry["bytes"]:
                raise PreparationError("Archive size does not match the source lock")
            shutil.copyfile(archive_path, archive)
        verify_archive(archive, entry)
        source = extract_archive(archive, stage / "extracted", entry["source_directory"])
        verify_identity(source, package, entry["version"])
        patches = prepare_build_requirements(source, entry)
        receipt = {
            "schema_version": 1,
            "package": package,
            "version": entry["version"],
            "source_dir": str(final_source),
            "provenance_path": str(final_source / PROVENANCE_FILENAME),
            "source_lock_sha256": sha256_file(lock_path),
            "source_archive": {
                key: entry[key] for key in ("url", "filename", "sha256", "bytes")
            },
            "metadata_url": entry["metadata_url"],
            "targets": lock["targets"],
            "build_environment": entry["build"].get("environment", {}),
            "patches": patches,
            "android_compatibility_verified": False,
        }
        if "paired_requirement" in entry:
            receipt["paired_requirement"] = entry["paired_requirement"]
        with (source / PROVENANCE_FILENAME).open("x", encoding="utf-8", newline="\n") as output:
            json.dump(receipt, output, indent=2, sort_keys=True)
            output.write("\n")
        source.rename(final_source)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Verify and safely prepare one locked sdist")
    prepare.add_argument("--package", required=True)
    prepare.add_argument("--dest", required=True, type=Path)
    prepare.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    prepare.add_argument(
        "--archive", type=Path, help="Optional local copy; still size/SHA-256 verified"
    )
    prepare.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        receipt = prepare_package(
            args.package, args.dest, lock_path=args.lock,
            archive_path=args.archive, timeout=args.timeout,
        )
    except (PreparationError, OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"Source preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
