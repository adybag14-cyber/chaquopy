#!/usr/bin/env python3
"""Assemble an offline, hash-verified Android wheelhouse without source builds.

Requires packaging (pinned by requirements-build.txt). Custom wheels must come
from the matching successful workflow artifacts. Static audits and dependency
closure do not establish Android runtime support.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

try:
    from . import audit_wheels as auditor
    from .verify_app_report import validate as validate_runtime_report
except ImportError:  # Direct script execution.
    import audit_wheels as auditor
    from verify_app_report import validate as validate_runtime_report


HERE = Path(__file__).resolve().parent
DEFAULT_REQUIREMENTS = HERE / "android/requirements.txt"
DEFAULT_LOCK = HERE / "sources.lock.json"
ABIS = ("arm64-v8a", "x86_64")
CUSTOM = {"jiter", "pydantic-core", "msgpack"}
# The compatibility app pins Chaquopy 17.0.0, whose bundled micro releases
# differ from the newer build-host Python. Check against the app interpreter.
RUNTIME_PYTHON = {"3.13": "3.13.9", "3.14": "3.14.0"}
MAX_JSON = 8 * 1024 * 1024
MAX_WHEEL = 512 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class AssemblyError(ValueError):
    """Untrusted input or unavailable dependency prevented assembly."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssemblyError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> tuple[dict, bytes]:
    require(
        path.is_file() and not path.is_symlink(), f"missing or linked JSON receipt: {path.name}"
    )
    require(path.stat().st_size <= MAX_JSON, f"JSON receipt too large: {path.name}")
    data = path.read_bytes()
    value = json.loads(data)
    require(isinstance(value, dict), f"JSON receipt is not an object: {path.name}")
    return value, data


def pinned_requirements(path: Path) -> tuple[dict[str, str], bytes]:
    data = path.read_bytes()
    result: dict[str, str] = {}
    for line in data.decode("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.!+]*)", line)
        require(
            match is not None,
            f"requirement must be one exact version without options/markers: {line}",
        )
        assert match is not None
        name = auditor.canonical_name(match[1])
        require(name not in result, f"duplicate pinned package: {name}")
        result[name] = match[2]
    require(CUSTOM <= set(result), "requirements must include jiter, pydantic-core and msgpack")
    return result, data


def validate_url(url: str) -> None:
    require(isinstance(url, str), "download URL must be a string")
    parsed = urlsplit(url)
    require(
        parsed.scheme == "https" and not parsed.query and not parsed.fragment,
        f"download must be HTTPS without query or fragment: {url!r}",
    )
    if parsed.netloc == "pypi.org":
        require(
            bool(re.fullmatch(r"/pypi/[A-Za-z0-9_.%-]+/[A-Za-z0-9_.!+%-]+/json", parsed.path)),
            "unexpected PyPI metadata URL",
        )
    else:
        require(
            parsed.netloc == "files.pythonhosted.org" and parsed.path.startswith("/packages/"),
            f"not an official PyPI download host/path: {url!r}",
        )
        auditor.canonical_path(unquote(parsed.path).lstrip("/"))


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        validate_url(newurl)
        # Metadata redirects must remain metadata; file redirects must remain
        # file downloads. Validate before making the redirected request.
        require(
            urlsplit(newurl).netloc == urlsplit(request.full_url).netloc,
            "cross-host PyPI redirect rejected",
        )
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_bytes(url: str, limit: int) -> bytes:
    validate_url(url)
    request = Request(url, headers={"User-Agent": "hermes-chaquopy-wheelhouse/1"})
    with build_opener(OfficialRedirects()).open(request, timeout=60) as response:
        validate_url(response.geturl())
        chunks, total = [], 0
        while chunk := response.read(min(1024 * 1024, limit + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            require(total <= limit, "download exceeds declared byte limit")
        return b"".join(chunks)


def checked_artifact(root: Path, name: str) -> Path:
    directory = root / name
    require(
        directory.is_dir() and not directory.is_symlink(), f"missing artifact directory: {name}"
    )
    require(directory.resolve().parent == root.resolve(), f"artifact escapes download root: {name}")
    for path in directory.rglob("*"):
        require(
            not path.is_symlink() and path.resolve().is_relative_to(directory.resolve()),
            f"linked/escaping artifact entry: {path.name}",
        )
    return directory


def check_provenance(receipt: dict, package: str, entry: dict, lock: dict, lock_hash: str) -> None:
    require(receipt.get("schema_version") == 1, "unsupported source provenance schema")
    require(
        auditor.canonical_name(receipt.get("package", "")) == package
        and receipt.get("version") == entry["version"],
        "source provenance identity mismatch",
    )
    require(receipt.get("source_lock_sha256") == lock_hash, "source provenance lock mismatch")
    require(
        receipt.get("source_archive")
        == {key: entry[key] for key in ("url", "filename", "sha256", "bytes")},
        "source archive provenance differs from lock",
    )
    require(
        receipt.get("metadata_url") == entry["metadata_url"]
        and receipt.get("targets") == lock["targets"],
        "source provenance metadata/targets mismatch",
    )
    require(
        receipt.get("build_environment") == entry["build"].get("environment", {}),
        "source build environment mismatch",
    )
    require(
        receipt.get("android_compatibility_verified") is False,
        "source preparation must not claim runtime compatibility",
    )
    if "paired_requirement" in entry:
        require(
            receipt.get("paired_requirement") == entry["paired_requirement"],
            "paired requirement mismatch",
        )
    patches = receipt.get("patches")
    if package == "msgpack":
        require(
            patches == [] and receipt["build_environment"] == {"MSGPACK_PUREPYTHON": "1"},
            "msgpack must use the unmodified official pure-Python variant",
        )
    else:
        require(
            isinstance(patches, list) and len(patches) == 1 and isinstance(patches[0], dict),
            "expected one pinned build-requirement patch",
        )
        patch = patches[0]
        require(
            {key: patch.get(key) for key in ("path", "kind", "from", "to")}
            == {
                "path": "pyproject.toml",
                "kind": "pin-build-requirement",
                "from": entry["build"]["original_requirement"],
                "to": entry["build"]["pinned_requirement"],
            },
            "unexpected source patch",
        )
        require(
            all(
                isinstance(patch.get(key), str) and SHA256.fullmatch(patch[key])
                for key in ("before_sha256", "after_sha256")
            )
            and patch["before_sha256"] != patch["after_sha256"],
            "invalid source patch hashes",
        )


def check_native_runtime(directory: Path, build: dict, package: str, python: str, abi: str) -> dict:
    """Bind measured x86 runtime facts without upgrading cross-ABI evidence."""
    runtime_path = directory / "runtime-report.json"
    runtime_keys = {key for key in build if key.startswith("runtime_")}
    if abi == "arm64-v8a":
        require(
            build.get("runtime_validation") == "not_run_cross_architecture",
            "ARM cross-architecture artifact must not claim runtime validation",
        )
        require(
            runtime_keys == {"runtime_validation"}
            and not runtime_path.exists()
            and not runtime_path.is_symlink(),
            "ARM cross-architecture artifact contains unexpected runtime evidence",
        )
        return {"runtime_evidence_verified": False}

    require(abi == "x86_64", "unsupported native runtime evidence ABI")
    require(
        build.get("runtime_validation") == "cibuildwheel-android-testbed-passed",
        "x86_64 artifact requires passing Android runtime validation",
    )
    require(
        runtime_keys
        == {
            "runtime_validation",
            "runtime_report_sha256",
            "runtime_android_api",
            "runtime_page_size",
            "runtime_python",
        },
        "x86_64 artifact requires the complete runtime report binding",
    )
    expected_hash = build["runtime_report_sha256"]
    require(
        isinstance(expected_hash, str) and SHA256.fullmatch(expected_hash),
        "invalid runtime report SHA-256 binding",
    )
    api, page_size, runtime_python = (
        build["runtime_android_api"],
        build["runtime_page_size"],
        build["runtime_python"],
    )
    require(
        type(api) is int
        and api >= 24
        and type(page_size) is int
        and page_size in {4096, 16384, 65536}
        and isinstance(runtime_python, str),
        "invalid measured runtime API/page-size/Python metadata",
    )
    report, payload = read_json(runtime_path)
    require(digest(payload) == expected_hash, "runtime report SHA-256 mismatch")
    try:
        validate_runtime_report(
            report,
            python,
            abi,
            package_group=package,
            expected_api=api,
        )
    except ValueError as error:
        raise AssemblyError(str(error)) from error
    platform = report["platform"]
    require(
        platform["android_api_level"] == api
        and platform["page_size"] == page_size
        and platform["python"] == runtime_python,
        "runtime report/build metadata mismatch",
    )
    return {
        "runtime_evidence_verified": True,
        "runtime_report_sha256": expected_hash,
        "runtime_android_api": api,
        "runtime_page_size": page_size,
        "runtime_python": runtime_python,
        "runtime_package_group": package,
        "runtime_tests_passed": report["tests"]["run"],
    }


def copy_custom(
    directory: Path,
    stage: Path,
    package: str,
    python: str,
    abi: str | None,
    entry: dict,
    lock: dict,
    lock_hash: str,
) -> dict:
    provenance, provenance_bytes = read_json(directory / "source-provenance.json")
    check_provenance(provenance, package, entry, lock, lock_hash)
    evidence = {"source_provenance_sha256": digest(provenance_bytes)}
    if abi:
        build, build_bytes = read_json(directory / "build-report.json")
        require(
            build.get("schema") == "hermes-android-native-build-v1"
            and build.get("build_status") == "passed",
            "native build did not pass",
        )
        require(
            (build.get("package"), build.get("python"), build.get("abi"), build.get("android_api"))
            == (package, python, abi, 24),
            "native build target mismatch",
        )
        require(build.get("source_lock_sha256") == lock_hash, "native build lock mismatch")
        require(
            build.get("cibuildwheel_version") == "4.3.5"
            and build.get("rustc_version", "").startswith("rustc 1.88.0 "),
            "native build toolchain mismatch",
        )
        evidence.update(check_native_runtime(directory, build, package, python, abi))
        evidence["build_report_sha256"] = digest(build_bytes)
        evidence["reported_runtime_validation"] = build["runtime_validation"]
    report, report_bytes = read_json(directory / "wheel-audit.json")
    expected_target = (python, abi) if abi else ("3.14", "x86_64")
    require(
        report.get("schema") == 1 and report.get("passed") is True and report.get("errors") == [],
        "artifact static audit did not pass",
    )
    require(
        (report.get("python"), report.get("abi")) == expected_target,
        "artifact static audit target mismatch",
    )
    require(
        report.get("minimum_android_api") == 24
        and report.get("required_load_alignment") == 16384
        and report.get("runtime_tested") is False,
        "artifact static audit contract mismatch",
    )
    reported_wheels = report.get("wheels")
    require(
        isinstance(reported_wheels, list) and len(reported_wheels) == 1,
        "artifact audit must contain exactly one wheel",
    )
    wheel = reported_wheels[0]
    require(
        wheel.get("package") == package and wheel.get("version") == entry["version"],
        "artifact wheel identity mismatch",
    )
    filename = wheel.get("filename", "")
    require(auditor.WHEEL_NAME.fullmatch(filename) is not None, "invalid artifact wheel filename")
    wheel_dir = directory / "wheels"
    require(
        wheel_dir.is_dir() and {path.name for path in wheel_dir.iterdir()} == {filename},
        "artifact wheels directory does not match its audit",
    )
    require(
        type(wheel.get("bytes")) is int
        and 0 < wheel["bytes"] <= MAX_WHEEL
        and isinstance(wheel.get("sha256"), str)
        and SHA256.fullmatch(wheel["sha256"]),
        "invalid artifact wheel digest/size",
    )
    source = wheel_dir / filename
    require(
        source.is_file() and source.stat().st_size == wheel["bytes"],
        "artifact wheel byte count mismatch",
    )
    data = source.read_bytes()
    require(
        len(data) == wheel["bytes"] and digest(data) == wheel["sha256"],
        "artifact wheel SHA-256 mismatch",
    )
    require(not (stage / filename).exists(), "duplicate output wheel filename")
    (stage / filename).write_bytes(data)
    fresh = auditor.audit_wheel(stage / filename, python, abi or "x86_64")
    require(
        all(
            fresh[key] == wheel.get(key)
            for key in ("package", "version", "bytes", "sha256", "tags")
        ),
        "artifact static audit no longer matches its wheel",
    )
    if package == "msgpack":
        require(
            fresh["tags"] == ["py3-none-any"] and not fresh["native_libraries"],
            "msgpack artifact is not a genuine pure-Python wheel",
        )
    evidence["wheel_audit_sha256"] = digest(report_bytes)
    return {key: fresh[key] for key in ("filename", "package", "version", "bytes", "sha256")} | {
        "abis": [abi] if abi else list(ABIS),
        "classification": "custom-android-native" if abi else "custom-pure-python",
        "source": {
            "type": "github-actions-artifact",
            "artifact_name": directory.name,
            "source_archive": provenance["source_archive"],
            "evidence": evidence,
        },
    }


def pypi_candidates(metadata: dict, package: str, version: str, python: str) -> list[dict]:
    require(
        auditor.canonical_name(metadata.get("info", {}).get("name", "")) == package
        and metadata.get("info", {}).get("version") == version,
        "PyPI metadata identity mismatch",
    )
    require(isinstance(metadata.get("urls"), list), "PyPI file list is missing")
    candidates = []
    for entry in metadata["urls"]:
        if entry.get("packagetype") != "bdist_wheel" or entry.get("yanked"):
            continue
        match = auditor.WHEEL_NAME.fullmatch(entry.get("filename", ""))
        if (
            not match
            or auditor.canonical_name(match["distribution"]) != package
            or match["version"] != version
        ):
            continue
        if entry.get("requires_python") and not SpecifierSet(entry["requires_python"]).contains(
            RUNTIME_PYTHON[python]
        ):
            continue
        candidates.append(entry)
    return candidates


def select_pypi(
    metadata: dict, package: str, version: str, python: str
) -> list[tuple[dict, list[str]]]:
    candidates = pypi_candidates(metadata, package, version, python)
    if package != "aiohttp":
        pure = [entry for entry in candidates if entry["filename"].endswith("-py3-none-any.whl")]
        require(
            len(pure) == 1,
            f"expected one genuine pure-Python wheel for {package}=={version}, found {len(pure)}",
        )
        return [(pure[0], list(ABIS))]
    selected = []
    target = "cp" + python.replace(".", "")
    for abi in ABIS:
        matching = []
        for entry in candidates:
            match = auditor.WHEEL_NAME.fullmatch(entry["filename"])
            platform = re.fullmatch(
                r"android_([1-9][0-9]*)_" + re.escape(abi.replace("-", "_")), match["platform"]
            )
            if platform and match["python"] == match["abi"] == target and int(platform[1]) <= 24:
                matching.append((int(platform[1]), entry))
        require(
            bool(matching),
            f"no official {package} cp{python.replace('.', '')} {abi} Android API<=24 wheel",
        )
        best_api = max(api for api, _ in matching)
        best = [entry for api, entry in matching if api == best_api]
        require(len(best) == 1, f"ambiguous official {package} Android wheel for {abi}")
        selected.append((best[0], [abi]))
    return selected


def copy_official(stage: Path, package: str, version: str, python: str, fetch) -> list[dict]:
    metadata_url = f"https://pypi.org/pypi/{quote(package, safe='')}/{quote(version, safe='')}/json"
    raw_metadata = fetch(metadata_url, MAX_JSON)
    require(len(raw_metadata) <= MAX_JSON, "PyPI metadata exceeds byte limit")
    metadata = json.loads(raw_metadata)
    result = []
    for entry, abis in select_pypi(metadata, package, version, python):
        filename, url = entry["filename"], entry.get("url", "")
        validate_url(url)
        require(
            urlsplit(url).netloc == "files.pythonhosted.org"
            and unquote(urlsplit(url).path.rsplit("/", 1)[-1]) == filename,
            "PyPI wheel URL does not match filename",
        )
        expected_hash, size = entry.get("digests", {}).get("sha256"), entry.get("size")
        require(
            isinstance(expected_hash, str)
            and SHA256.fullmatch(expected_hash)
            and type(size) is int
            and 0 < size <= MAX_WHEEL,
            "invalid PyPI wheel digest/size",
        )
        payload = fetch(url, size)
        require(
            len(payload) == size and digest(payload) == expected_hash,
            "PyPI wheel byte count/SHA-256 mismatch",
        )
        require(not (stage / filename).exists(), "duplicate output wheel filename")
        (stage / filename).write_bytes(payload)
        result.append(
            {
                "filename": filename,
                "package": package,
                "version": version,
                "bytes": size,
                "sha256": expected_hash,
                "abis": abis,
                "classification": "official-android-native"
                if package == "aiohttp"
                else "official-pure-python",
                "source": {
                    "type": "pypi",
                    "url": url,
                    "metadata_url": metadata_url,
                    "metadata_sha256": digest(raw_metadata),
                },
            }
        )
    return result


def dependency_closure(
    view: Path, requirements: dict[str, str], python: str, abi: str
) -> list[dict]:
    metadata = {}
    for path in view.glob("*.whl"):
        match = auditor.WHEEL_NAME.fullmatch(path.name)
        name = auditor.canonical_name(match["distribution"])
        with zipfile.ZipFile(path) as archive:
            filename = f"{match['distribution']}-{match['version']}.dist-info/METADATA"
            metadata[name] = BytesParser().parsebytes(archive.read(filename))
    require(
        set(metadata) == set(requirements), "wheelhouse package set differs from exact requirements"
    )
    environment = default_environment() | {
        "os_name": "posix",
        "sys_platform": "android",
        "platform_system": "Android",
        "platform_machine": "aarch64" if abi == "arm64-v8a" else "x86_64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "implementation_version": RUNTIME_PYTHON[python],
        "python_version": python,
        "python_full_version": RUNTIME_PYTHON[python],
        "platform_release": "",
        "platform_version": "",
        "extra": "",
    }
    extras = {name: {""} for name in metadata}
    edges = set()
    pending = list(metadata)
    while pending:
        package = pending.pop()
        message = metadata[package]
        if message.get("Requires-Python"):
            require(
                SpecifierSet(message["Requires-Python"]).contains(RUNTIME_PYTHON[python]),
                f"{package} Requires-Python excludes the app interpreter",
            )
        for text in message.get_all("Requires-Dist", []):
            dependency = Requirement(text)
            if dependency.marker and not any(
                dependency.marker.evaluate(environment | {"extra": extra})
                for extra in extras[package]
            ):
                continue
            name = auditor.canonical_name(dependency.name)
            require(not dependency.url, f"direct-URL dependency is forbidden: {package}: {text}")
            require(
                name in requirements, f"offline closure missing dependency: {package} -> {name}"
            )
            require(
                dependency.specifier.contains(requirements[name], prereleases=True),
                f"pinned dependency conflict: {package} requires {text}, "
                f"selected {requirements[name]}",
            )
            edges.add((package, name, text))
            added = set(dependency.extras) - extras[name]
            if added:
                extras[name].update(added)
                pending.append(name)
    return [
        {"package": package, "dependency": dependency, "requirement": text}
        for package, dependency, text in sorted(edges)
    ]


def assemble(
    downloads: Path,
    python: str,
    output: Path,
    *,
    requirements_path: Path = DEFAULT_REQUIREMENTS,
    lock_path: Path = DEFAULT_LOCK,
    fetch=fetch_bytes,
) -> dict:
    require(python in RUNTIME_PYTHON, "Python must be 3.13 or 3.14")
    require(downloads.is_dir() and not downloads.is_symlink(), "downloads must be a real directory")
    require(not output.exists() and not output.is_symlink(), "output must be a new directory")
    require(
        not output.resolve().is_relative_to(downloads.resolve()),
        "output must not be inside downloads",
    )
    requirements, requirements_bytes = pinned_requirements(requirements_path)
    lock, lock_bytes = read_json(lock_path)
    require(
        lock.get("schema_version") == 1
        and lock.get("targets")
        == {
            "python": ["3.13", "3.14"],
            "android_api": 24,
            "abis": list(ABIS),
        },
        "unsupported source-lock target contract",
    )
    lock_hash = digest(lock_bytes)
    locked = {auditor.canonical_name(name): entry for name, entry in lock["packages"].items()}
    require(CUSTOM <= set(locked), "source lock does not include custom packages")
    for name in CUSTOM:
        require(
            requirements[name] == locked[name]["version"],
            f"custom source/requirement version mismatch: {name}",
        )
    if locked["pydantic-core"].get("paired_requirement"):
        paired = Requirement(locked["pydantic-core"]["paired_requirement"])
        require(
            paired.name in requirements and paired.specifier.contains(requirements[paired.name]),
            "Pydantic/core paired requirement is not pinned",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hermes-wheelhouse-", dir=output.parent) as temporary:
        stage = Path(temporary) / "wheels"
        stage.mkdir()
        wheels = []
        for package in ("jiter", "pydantic-core"):
            for abi in ABIS:
                directory = checked_artifact(downloads, f"hermes-native-{package}-{python}-{abi}")
                wheels.append(
                    copy_custom(
                        directory, stage, package, python, abi, locked[package], lock, lock_hash
                    )
                )
        directory = checked_artifact(downloads, "hermes-msgpack-pure-python")
        wheels.append(
            copy_custom(
                directory, stage, "msgpack", python, None, locked["msgpack"], lock, lock_hash
            )
        )
        for package, version in sorted(requirements.items()):
            if package not in CUSTOM:
                wheels.extend(copy_official(stage, package, version, python, fetch))
        audits, closures = {}, {}
        for abi in ABIS:
            view = Path(temporary) / abi
            view.mkdir()
            for wheel in wheels:
                if abi in wheel["abis"]:
                    shutil.copyfile(stage / wheel["filename"], view / wheel["filename"])
            audits[abi] = auditor.audit_directory(view, python, abi)
            require(
                audits[abi]["passed"],
                f"assembled {abi} static audit failed: {audits[abi]['errors']}",
            )
            closures[abi] = dependency_closure(view, requirements, python, abi)
        manifest = {
            "schema": "hermes-android-wheelhouse-v1",
            "passed": True,
            "python": python,
            "app_python_full_version": RUNTIME_PYTHON[python],
            "android_api": 24,
            "abis": list(ABIS),
            "runtime_tested": False,
            "scope": (
                "Verified source receipts, wheel integrity, static ELF audits and "
                "offline dependency closure; no runtime certification"
            ),
            "source_lock_sha256": lock_hash,
            "requirements_sha256": digest(requirements_bytes),
            "requirements": requirements,
            "wheels": sorted(wheels, key=lambda wheel: wheel["filename"]),
            "static_audits": audits,
            "dependency_closure": closures,
        }
        (stage / "wheelhouse-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        require(not output.exists(), "output appeared during assembly; refusing overwrite")
        stage.rename(output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", required=True, type=Path)
    parser.add_argument("--python", required=True, choices=tuple(RUNTIME_PYTHON))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = assemble(args.downloads, args.python, args.output)
    except (
        AssemblyError,
        auditor.AuditError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
    ) as error:
        print(f"Wheelhouse assembly rejected: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"output": str(args.output), "wheels": len(report["wheels"]), "runtime_tested": False},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
