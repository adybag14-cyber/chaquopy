#!/usr/bin/env python3
"""Validate upstream-built Python bootstrap ZIPs and stage a narrow Maven repo.

The upstream zipPython task, not this helper, compiles and packages the runtime.
Validation never imports or executes archived code. Each compiled module is
compared with the current source tree on a matching CPython minor, and every
selected dist-info file must match byte for byte. Timestamp/hash PYC headers may
vary; the complete code object, including nested code and filenames, may not.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import marshal
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import struct
import sys
import tempfile
import types
import zipfile


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path("product/runtime/src/main/python")
IMPORTER = "java/android/importer.py"
INPUT_FILES = (
    "VERSION.txt",
    "compat/hermes/sources.lock.json",
    "product/runtime/build.gradle",
    "product/buildSrc/src/main/java/com/chaquo/python/internal/Common.java",
)
SCHEMA = "hermes-chaquopy-bootstrap-v1"
APK_ENTRY = "assets/chaquopy/bootstrap.imy"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
CODE_FIELDS = (
    "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
    "co_stacksize", "co_flags", "co_code", "co_consts", "co_names", "co_varnames",
    "co_filename", "co_name", "co_qualname", "co_firstlineno", "co_linetable",
    "co_exceptiontable", "co_freevars", "co_cellvars",
)


class BootstrapError(ValueError):
    """Archive, source or receipt failed its fail-closed binding checks."""


def require(condition, message):
    if not condition:
        raise BootstrapError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def read_file(path, limit=MAX_MEMBER_BYTES):
    require(path.is_file() and not path.is_symlink(), f"Missing or linked input: {path}")
    require(path.stat().st_size <= limit, f"Input exceeds byte limit: {path}")
    data = path.read_bytes()
    require(len(data) <= limit, f"Input grew beyond byte limit: {path}")
    return data


def host_python(python):
    require(python in ("3.13", "3.14"), "Python must be 3.13 or 3.14")
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    require(sys.implementation.name == "cpython" and actual == python,
            f"Use CPython {python} to validate its bytecode; this host is {actual}")


def safe_path(name):
    require(isinstance(name, str) and name and not name.startswith(("/", "\\"))
            and "\\" not in name and ":" not in name
            and not any(ord(char) < 32 for char in name), "Unsafe archive path")
    path = PurePosixPath(name)
    require(path.parts and ".." not in path.parts and str(path) == name.rstrip("/")
            and all(not part.endswith((".", " ")) for part in path.parts),
            f"Noncanonical archive path: {name!r}")
    return path


def source_inputs(repo_root):
    root = repo_root.resolve()
    source_root = root / RUNTIME
    require(source_root.is_dir() and not source_root.is_symlink(), "Missing runtime source tree")
    inventory, contents = [], {}
    for path in sorted(source_root.rglob("*")):
        require(not path.is_symlink(), f"Linked runtime source: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source_root).as_posix()
        if path.suffix == ".py":
            kind, archive_path = "python", relative + "c"
        elif any(part.endswith(".dist-info") for part in path.relative_to(source_root).parts):
            kind, archive_path = "dist-info", relative
        else:
            continue
        safe_path(relative)
        data = read_file(path)
        contents[relative] = data
        inventory.append({"path": relative, "kind": kind, "bytes": len(data),
                          "sha256": sha256(data), "archive_path": archive_path})
    require(IMPORTER in contents, "Missing production java/android/importer.py")
    require(any(item["kind"] == "dist-info" and item["path"].endswith("/LICENSE")
                for item in inventory), "Missing bootstrap dist-info license")
    inputs = []
    for name in INPUT_FILES:
        path = root / name
        data = read_file(path)
        inputs.append({"path": name, "bytes": len(data), "sha256": sha256(data)})
    version = (root / "VERSION.txt").read_text(encoding="utf-8").strip()
    require(re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version),
            "VERSION.txt must contain one safe Maven version")
    inventory_hash = sha256(canonical_json(inventory))
    return {
        "version": version,
        "input_files": inputs,
        "input_sha256": sha256(canonical_json({"files": inputs, "sources": inventory})),
        "source_lock_sha256": next(item["sha256"] for item in inputs
                                   if item["path"] == "compat/hermes/sources.lock.json"),
        "source_inventory": inventory,
        "source_inventory_sha256": inventory_hash,
        "importer_source_sha256": sha256(contents[IMPORTER]),
    }, contents


def canonical_constant(value):
    if isinstance(value, types.CodeType):
        return {"code": {name: canonical_constant(getattr(value, name)) for name in CODE_FIELDS}}
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if type(value) in (bool, int, str):
        return [type(value).__name__, value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, float):
        return ["float", struct.pack(">d", value).hex()]
    if isinstance(value, complex):
        return ["complex", struct.pack(">dd", value.real, value.imag).hex()]
    if isinstance(value, slice):
        return ["slice", [canonical_constant(item)
                          for item in (value.start, value.stop, value.step)]]
    if isinstance(value, tuple):
        return ["tuple", [canonical_constant(item) for item in value]]
    if isinstance(value, frozenset):
        items = [canonical_constant(item) for item in value]
        return ["frozenset", sorted(items, key=canonical_json)]
    raise BootstrapError(f"Unsupported compiled constant: {type(value).__name__}")


def validate_pyc(data, source, source_path):
    require(len(data) >= 16 and data[:4] == importlib.util.MAGIC_NUMBER,
            f"PYC magic/minor mismatch: {source_path}")
    flags = int.from_bytes(data[4:8], "little")
    require(flags in (0, 1, 3), f"Invalid PYC flags: {source_path}")
    if flags & 1:
        require(data[8:16] == importlib.util.source_hash(source),
                f"PYC source hash mismatch: {source_path}")
    else:
        require(int.from_bytes(data[12:16], "little") == len(source),
                f"PYC source length mismatch: {source_path}")
    stream = io.BytesIO(data[16:])
    try:
        actual = marshal.load(stream)
    except (EOFError, ValueError, TypeError) as error:
        raise BootstrapError(f"Malformed PYC marshal data: {source_path}") from error
    require(isinstance(actual, types.CodeType) and stream.tell() == len(data) - 16,
            f"PYC must contain exactly one code object: {source_path}")
    filename = actual.co_filename
    windows_path = source_path.replace("/", "\\")
    require(filename in (source_path, "./" + source_path, windows_path, ".\\" + windows_path),
            f"Unexpected PYC source filename: {source_path}")
    expected = compile(source, filename, "exec", dont_inherit=True, optimize=0)
    actual_code = canonical_json(canonical_constant(actual))
    expected_code = canonical_json(canonical_constant(expected))
    require(actual_code == expected_code, f"PYC does not match current fork source: {source_path}")
    return {"source_path": source_path, "co_filename": filename,
            "code_sha256": sha256(actual_code), "pyc_sha256": sha256(data), "pyc_bytes": len(data)}


def inspect_archive(data, python, *, repo_root=ROOT):
    host_python(python)
    require(0 < len(data) <= MAX_ARCHIVE_BYTES, "Invalid bootstrap archive byte count")
    context, contents = source_inputs(repo_root)
    expected = {item["archive_path"]: item for item in context["source_inventory"]}
    expected_dirs = {str(parent) for name in expected
                     for parent in PurePosixPath(name).parents if str(parent) != "."}
    bytecode = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        require(len(infos) <= MAX_MEMBERS, "Too many bootstrap ZIP entries")
        seen, files, expanded = set(), {}, 0
        for entry in infos:
            path = safe_path(entry.filename)
            key = str(path).casefold()
            require(key not in seen, f"Duplicate bootstrap ZIP entry: {entry.filename}")
            seen.add(key)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(mode in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG),
                    f"Linked/special bootstrap ZIP entry: {entry.filename}")
            require(not entry.flag_bits & 1, "Encrypted bootstrap entries are not allowed")
            require(entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    "Unsupported bootstrap ZIP compression")
            require(0 <= entry.file_size <= MAX_MEMBER_BYTES, "Bootstrap member is too large")
            expanded += entry.file_size
            require(expanded <= MAX_EXPANDED_BYTES, "Bootstrap expanded byte limit exceeded")
            if entry.is_dir():
                require(str(path) in expected_dirs and entry.file_size == 0,
                        f"Unexpected bootstrap directory: {entry.filename}")
            else:
                require(entry.filename in expected,
                        f"Unexpected bootstrap file: {entry.filename}")
                files[entry.filename] = entry
        require(set(files) == set(expected), "Bootstrap archive is missing source-selected files")
        for name, item in sorted(expected.items()):
            payload = archive.read(files[name])
            source = contents[item["path"]]
            if item["kind"] == "python":
                bytecode.append(validate_pyc(payload, source, item["path"]))
            else:
                require(payload == source, f"Bootstrap dist-info bytes differ: {name}")
    importer = next(item for item in bytecode if item["source_path"] == IMPORTER)
    return {**context, "archive_sha256": sha256(data), "archive_bytes": len(data),
            "pyc_magic_hex": importlib.util.MAGIC_NUMBER.hex(),
            "bytecode_inventory": bytecode, "importer_code_sha256": importer["code_sha256"]}


def maven_paths(version, python):
    prefix = f"maven/com/chaquo/python/runtime/bootstrap/{version}/bootstrap-{version}"
    return prefix + f"-{python}.imy", prefix + ".pom"


def pom_bytes(version):
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>com.chaquo.python.runtime</groupId>\n"
            "  <artifactId>bootstrap</artifactId>\n"
            f"  <version>{version}</version>\n"
            "  <packaging>pom</packaging>\n"
            "  <name>Source-built Hermes compatibility bootstrap</name>\n"
            "</project>\n").encode("utf-8")


def stage(archive, python, output, *, repo_root=ROOT):
    require(not output.exists() and not output.is_symlink(), "Output must be a new directory")
    data = read_file(archive, MAX_ARCHIVE_BYTES)
    inspected = inspect_archive(data, python, repo_root=repo_root)
    archive_name, pom_name = maven_paths(inspected["version"], python)
    pom = pom_bytes(inspected["version"])
    receipt = {"schema": SCHEMA, "python": python, "compiler_python": platform.python_version(),
               "maven_repo": "maven", "archive_path": archive_name, "pom_path": pom_name,
               "pom_sha256": sha256(pom), **inspected}
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hermes-bootstrap-", dir=output.parent) as temporary:
        staging = Path(temporary)
        archive_output = staging / archive_name
        archive_output.parent.mkdir(parents=True)
        archive_output.write_bytes(data)
        (staging / pom_name).write_bytes(pom)
        (staging / "receipt.json").write_bytes(canonical_json(receipt) + b"\n")
        require(not output.exists() and not output.is_symlink(), "Output appeared during staging")
        staging.rename(output)
    return {**receipt, "receipt_path": str(output / "receipt.json"),
            "maven_repo_path": str(output / "maven")}


def unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate receipt key: {key}")
        result[key] = value
    return result


def validate_receipt(receipt_path, *, repo_root=ROOT, expected_python=None):
    receipt_path = Path(receipt_path)
    receipt = json.loads(read_file(receipt_path, MAX_RECEIPT_BYTES),
                         object_pairs_hook=unique_json_pairs)
    require(isinstance(receipt, dict) and receipt.get("schema") == SCHEMA,
            "Unsupported bootstrap receipt")
    python = receipt.get("python")
    require(expected_python is None or python == expected_python, "Receipt Python mismatch")
    host_python(python)
    compiler = receipt.get("compiler_python")
    require(isinstance(compiler, str) and re.fullmatch(re.escape(python) + r"\.\d+", compiler),
            "Receipt compiler Python mismatch")
    context, _ = source_inputs(repo_root)
    archive_name, pom_name = maven_paths(context["version"], python)
    require(receipt.get("archive_path") == archive_name and receipt.get("pom_path") == pom_name
            and receipt.get("maven_repo") == "maven", "Receipt Maven coordinates mismatch")
    directory = receipt_path.resolve().parent
    for name in (archive_name, pom_name):
        path = directory / name
        require(path.resolve().is_relative_to(directory)
                and all(not parent.is_symlink() for parent in path.parents
                        if parent != directory and parent.is_relative_to(directory)),
                "Linked or escaping Maven artifact path")
    data = read_file(directory / archive_name, MAX_ARCHIVE_BYTES)
    inspected = inspect_archive(data, python, repo_root=repo_root)
    pom = read_file(directory / pom_name)
    require(pom == pom_bytes(context["version"]), "Maven POM differs from current source version")
    expected = {"schema": SCHEMA, "python": python, "compiler_python": compiler,
                "maven_repo": "maven", "archive_path": archive_name, "pom_path": pom_name,
                "pom_sha256": sha256(pom), **inspected}
    require(receipt == expected, "Bootstrap receipt differs from current source/archive evidence")
    return {**receipt, "receipt_path": str(receipt_path.resolve()),
            "maven_repo_path": str(directory / "maven")}


def verify_apk(apk, receipt_path, *, python=None, repo_root=ROOT):
    receipt = validate_receipt(receipt_path, repo_root=repo_root, expected_python=python)
    require(apk.is_file() and not apk.is_symlink(), "Missing or linked APK")
    with zipfile.ZipFile(apk) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename == APK_ENTRY]
        require(len(entries) == 1, "APK must contain exactly one assets/chaquopy/bootstrap.imy")
        entry = entries[0]
        require(not entry.is_dir() and not entry.flag_bits & 1
                and entry.file_size == receipt["archive_bytes"]
                and entry.file_size <= MAX_ARCHIVE_BYTES,
                "APK bootstrap entry size/type mismatch")
        data = archive.read(entry)
    require(sha256(data) == receipt["archive_sha256"],
            "APK bootstrap does not match the source-built archive (stock fallback rejected)")
    inner = inspect_archive(data, receipt["python"], repo_root=repo_root)
    require(inner["importer_code_sha256"] == receipt["importer_code_sha256"],
            "APK importer bytecode differs from current fork")
    apk_hash = hashlib.sha256()
    with apk.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            apk_hash.update(chunk)
    return {"schema": "hermes-chaquopy-bootstrap-apk-v1", "verified": True,
            "python": receipt["python"], "version": receipt["version"],
            "apk_sha256": apk_hash.hexdigest(), "apk_bytes": apk.stat().st_size,
            "archive_sha256": receipt["archive_sha256"],
            "importer_source_sha256": receipt["importer_source_sha256"],
            "importer_code_sha256": inner["importer_code_sha256"],
            "source_inventory_sha256": inner["source_inventory_sha256"],
            "receipt_sha256": sha256(read_file(Path(receipt_path), MAX_RECEIPT_BYTES))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage")
    staging.add_argument("--archive", type=Path, required=True)
    staging.add_argument("--python", choices=("3.13", "3.14"), required=True)
    staging.add_argument("--output", type=Path, required=True)
    apk = commands.add_parser("verify-apk")
    apk.add_argument("--apk", type=Path, required=True)
    apk.add_argument("--receipt", type=Path, required=True)
    apk.add_argument("--python", choices=("3.13", "3.14"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            result = stage(args.archive, args.python, args.output)
        else:
            result = verify_apk(args.apk, args.receipt, python=args.python)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, zipfile.BadZipFile) as error:
        print(f"Bootstrap validation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
