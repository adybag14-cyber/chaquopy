#!/usr/bin/env python3
"""Fail-closed, stdlib-only audit of the Hermes Android wheel set.

This checks packaging and ELF structure, not whether a library actually loads or
works on Android. Emulator/device tests are always a separate gate.
"""

from __future__ import annotations

import argparse
import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import itertools
import json
from pathlib import Path
import re
import stat
import struct
import sys
import zipfile


class AuditError(ValueError):
    """An archive does not meet the declared compatibility contract."""


MACHINES = {"arm64-v8a": 183, "x86_64": 62}
PAGE_SIZE = 16384
MIN_ANDROID_API = 24
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_WHEEL_BYTES = 2 * 1024 * 1024 * 1024
# Public NDK libraries available by API 24. libc++_shared and third-party
# libraries must be bundled; host glibc libraries are deliberately absent.
ANDROID_LIBRARIES = {
    "libc.so",
    "libm.so",
    "libdl.so",
    "liblog.so",
    "libandroid.so",
    "libz.so",
    "libjnigraphics.so",
    "libEGL.so",
    "libGLESv1_CM.so",
    "libGLESv2.so",
    "libGLESv3.so",
    "libOpenSLES.so",
    "libOpenMAXAL.so",
    "libvulkan.so",
}
REQUIRED_EXTENSIONS = {"jiter": "jiter", "pydantic-core": "_pydantic_core"}
WHEEL_NAME = re.compile(
    r"(?P<distribution>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)"
    r"(?:-(?P<build>[0-9][A-Za-z0-9_]*))?"
    r"-(?P<python>[A-Za-z0-9_.]+)-(?P<abi>[A-Za-z0-9_.]+)"
    r"-(?P<platform>[A-Za-z0-9_.]+)\.whl\Z"
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def canonical_path(value: str, *, directory: bool = False) -> str:
    require(bool(value) and "\x00" not in value, "empty or NUL archive path")
    require("\\" not in value and ":" not in value, f"non-portable path: {value!r}")
    path = value[:-1] if directory and value.endswith("/") else value
    require(
        all(part not in {"", ".", ".."} for part in path.split("/")),
        f"non-canonical or traversal path: {value!r}",
    )
    return path


def unpack_at(fmt: str, data: bytes, offset: int, context: str) -> tuple:
    size = struct.calcsize(fmt)
    require(0 <= offset <= len(data) - size, f"truncated {context}")
    return struct.unpack_from(fmt, data, offset)


def audit_elf(data: bytes, abi: str, name: str) -> dict:
    require(len(data) >= 64 and data[:4] == b"\x7fELF", f"invalid ELF: {name}")
    require(data[4:7] == b"\x02\x01\x01", f"ELF must be 64-bit little-endian v1: {name}")
    require(data[7] in {0, 3}, f"unsupported ELF OS ABI: {name}")
    header = unpack_at("<HHIQQQIHHHHHH", data, 16, "ELF header")
    kind, machine, version = header[:3]
    phoff, ehsize, phentsize, phnum = header[4], header[7], header[8], header[9]
    require(kind == 3 and version == 1, f"ELF is not an ET_DYN library: {name}")
    require(machine == MACHINES[abi], f"ELF machine {machine} does not match {abi}: {name}")
    require(
        ehsize == 64 and phentsize == 56 and 0 < phnum < 65535,
        f"unsupported ELF program-header layout: {name}",
    )
    require(
        phoff >= ehsize and phoff + phnum * phentsize <= len(data),
        f"truncated ELF program headers: {name}",
    )
    loads, dynamics = [], []
    for index in range(phnum):
        values = unpack_at("<IIQQQQQQ", data, phoff + index * phentsize, "ELF segment")
        segment = dict(
            zip(("type", "flags", "offset", "vaddr", "paddr", "filesz", "memsz", "align"), values)
        )
        require(
            segment["offset"] + segment["filesz"] <= len(data), f"ELF segment outside file: {name}"
        )
        if segment["type"] == 1:
            alignment = segment["align"]
            require(segment["filesz"] <= segment["memsz"], f"ELF filesz exceeds memsz: {name}")
            require(
                alignment >= PAGE_SIZE and alignment & (alignment - 1) == 0,
                f"PT_LOAD alignment is not a power of two >= 16384: {name}",
            )
            require(
                (segment["vaddr"] - segment["offset"]) % alignment == 0,
                f"PT_LOAD virtual address/file offset are not congruent: {name}",
            )
            loads.append(segment)
        elif segment["type"] == 2:
            dynamics.append(segment)
    require(bool(loads), f"ELF has no PT_LOAD: {name}")
    require(len(dynamics) == 1, f"ELF must have exactly one PT_DYNAMIC: {name}")
    dynamic = dynamics[0]
    require(
        any(
            load["offset"] <= dynamic["offset"]
            and dynamic["offset"] + dynamic["filesz"] <= load["offset"] + load["filesz"]
            and dynamic["vaddr"] - load["vaddr"] == dynamic["offset"] - load["offset"]
            for load in loads
        ),
        f"ELF dynamic table is not mapped by PT_LOAD: {name}",
    )
    require(dynamic["filesz"] % 16 == 0, f"mis-sized ELF dynamic table: {name}")
    needed, sonames, string_tables, string_sizes, symbol_tables, symbol_sizes = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    terminated = False
    for offset in range(dynamic["offset"], dynamic["offset"] + dynamic["filesz"], 16):
        tag, value = unpack_at("<qQ", data, offset, "ELF dynamic entry")
        if tag == 0:
            terminated = True
            break
        if tag == 1:
            needed.append(value)
        elif tag == 5:
            string_tables.append(value)
        elif tag == 6:
            symbol_tables.append(value)
        elif tag == 10:
            string_sizes.append(value)
        elif tag == 11:
            symbol_sizes.append(value)
        elif tag == 14:
            sonames.append(value)
    require(terminated, f"ELF dynamic table is not terminated: {name}")
    require(len(sonames) <= 1, f"duplicate DT_SONAME: {name}")
    require(
        len(string_tables) == len(string_sizes) == 1,
        f"ELF must have one DT_STRTAB and DT_STRSZ: {name}",
    )
    address, size = string_tables[0], string_sizes[0]
    matches = [
        load
        for load in loads
        if load["vaddr"] <= address and address + size <= load["vaddr"] + load["filesz"]
    ]
    require(len(matches) == 1 and size > 0, f"ELF string table is not file-backed: {name}")
    load = matches[0]
    start = load["offset"] + address - load["vaddr"]
    strings = data[start : start + size]

    # Dynamic symbol tables survive normal stripping. Merely renaming an ELF to
    # jiter.so must not satisfy the extension requirement.
    exports: set[str] = set()
    shoff, shentsize, shnum = header[5], header[10], header[11]
    if shoff or shnum:
        require(
            shoff >= ehsize and shentsize == 64 and shnum > 0,
            f"unsupported ELF section-header layout: {name}",
        )
        require(shoff + shnum * shentsize <= len(data), f"truncated ELF section headers: {name}")
        sections = [
            unpack_at("<IIQQQQIIQQ", data, shoff + index * shentsize, "ELF section")
            for index in range(shnum)
        ]
        for section in sections:
            if section[1] != 11:  # SHT_DYNSYM
                continue
            require(
                symbol_tables == [section[3]] and symbol_sizes == [24],
                f"ELF symbols do not match DT_SYMTAB/DT_SYMENT: {name}",
            )
            require(
                section[9] == 24 and section[5] % 24 == 0 and section[6] < shnum,
                f"invalid ELF dynamic symbol table: {name}",
            )
            require(section[4] + section[5] <= len(data), f"truncated ELF symbols: {name}")
            string_section = sections[section[6]]
            require(
                string_section[1] == 3 and string_section[4] + string_section[5] <= len(data),
                f"invalid ELF symbol string table: {name}",
            )
            require(
                string_section[3] == address
                and string_section[4] == start
                and string_section[5] == size,
                f"ELF symbol strings do not match DT_STRTAB: {name}",
            )
            require(
                any(
                    load["vaddr"] <= section[3]
                    and section[3] + section[5] <= load["vaddr"] + load["filesz"]
                    and section[4] - load["offset"] == section[3] - load["vaddr"]
                    for load in loads
                ),
                f"ELF symbol table is not file-backed: {name}",
            )
            symbol_strings = data[string_section[4] : string_section[4] + string_section[5]]
            for offset in range(section[4], section[4] + section[5], 24):
                symbol, info, visibility, section_index, _, _ = unpack_at(
                    "<IBBHQQ", data, offset, "ELF symbol"
                )
                require(symbol < len(symbol_strings), f"ELF symbol string outside table: {name}")
                end = symbol_strings.find(b"\x00", symbol)
                require(end >= 0, f"unterminated ELF symbol: {name}")
                if (
                    section_index != 0
                    and info >> 4 in {1, 2}
                    and info & 15 in {0, 2}
                    and visibility & 3 in {0, 3}
                ):
                    raw_symbol = symbol_strings[symbol:end]
                    if raw_symbol.startswith(b"PyInit_"):
                        try:
                            exports.add(raw_symbol.decode("ascii"))
                        except UnicodeError as error:
                            raise AuditError(
                                f"invalid Python initializer symbol: {name}"
                            ) from error

    def library_name(offset: int) -> str:
        require(0 <= offset < len(strings), f"ELF string offset outside table: {name}")
        end = strings.find(b"\x00", offset)
        require(end >= 0, f"unterminated ELF library name: {name}")
        try:
            value = strings[offset:end].decode("ascii")
        except UnicodeError as error:
            raise AuditError(f"non-ASCII ELF library name: {name}") from error
        require(
            bool(re.fullmatch(r"[A-Za-z0-9_+.-]+", value)) and value not in {".", ".."},
            f"unsafe ELF library name {value!r}: {name}",
        )
        return value

    return {
        "path": name,
        "machine": machine,
        "minimum_load_alignment": min(load["align"] for load in loads),
        "needed": [library_name(offset) for offset in needed],
        "soname": library_name(sonames[0]) if sonames else None,
        "python_initializers": sorted(exports),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_archive(path: Path, payload: bytes | None = None) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(payload) if payload is not None else path) as archive:
        require(
            sum(item.file_size for item in archive.infolist()) <= MAX_WHEEL_BYTES,
            "wheel exceeds uncompressed size limit",
        )
        for item in archive.infolist():
            canonical_path(item.orig_filename, directory=item.orig_filename.endswith("/"))
            require(item.orig_filename == item.filename, "ZIP filename was truncated")
            name = canonical_path(item.filename, directory=item.is_dir())
            require(name not in seen, f"duplicate ZIP path: {name}")
            seen.add(name)
            kind = stat.S_IFMT(item.external_attr >> 16)
            allowed = {0, stat.S_IFDIR} if item.is_dir() else {0, stat.S_IFREG}
            require(kind in allowed, f"symlink or special ZIP entry: {name}")
            require(not item.flag_bits & 1, f"encrypted ZIP entry: {name}")
            require(item.file_size <= MAX_MEMBER_BYTES, f"ZIP member exceeds size limit: {name}")
            if item.is_dir():
                require(item.file_size == 0, f"nonempty ZIP directory: {name}")
            else:
                files[name] = archive.read(item)
    require(bool(files), "empty wheel")
    for name in files:
        parts = name.split("/")
        require(
            not any("/".join(parts[:index]) in files for index in range(1, len(parts))),
            f"ZIP file/directory collision: {name}",
        )
    return files


def verify_record(files: dict[str, bytes], dist_info: str) -> None:
    record = f"{dist_info}/RECORD"
    require(record in files, "missing RECORD")
    try:
        rows = list(csv.reader(io.StringIO(files[record].decode("utf-8")), strict=True))
    except (UnicodeError, csv.Error) as error:
        raise AuditError(f"invalid RECORD: {error}") from error
    seen: set[str] = set()
    signatures = {f"{dist_info}/RECORD.jws", f"{dist_info}/RECORD.p7s"}
    for row in rows:
        require(len(row) == 3, "RECORD row must have exactly three fields")
        name, digest, size = row
        canonical_path(name)
        require(name not in seen, f"duplicate RECORD path: {name}")
        seen.add(name)
        require(name in files, f"RECORD references missing file: {name}")
        require(name not in signatures, f"signature must not be listed in RECORD: {name}")
        if name == record:
            require(digest == size == "", "RECORD must not hash itself")
            continue
        require(
            size.isascii() and size.isdigit() and len(size) <= 20 and int(size) == len(files[name]),
            f"RECORD size mismatch: {name}",
        )
        algorithm, separator, encoded = digest.partition("=")
        require(
            separator == "=" and algorithm in {"sha256", "sha384", "sha512"},
            f"RECORD requires a strong hash: {name}",
        )
        expected = base64.urlsafe_b64encode(hashlib.new(algorithm, files[name]).digest())
        require(encoded == expected.rstrip(b"=").decode("ascii"), f"RECORD hash mismatch: {name}")
    require(seen == set(files) - signatures, "RECORD does not cover the exact wheel file set")


def compatible_tag(tag: tuple[str, str, str], python: str, abi: str) -> bool:
    interpreter, binary_abi, platform = tag
    target = "cp" + python.replace(".", "")
    if platform == "any":
        return (
            interpreter in {"py3", "py" + python.replace(".", ""), target} and binary_abi == "none"
        )
    match = re.fullmatch(r"android_([1-9][0-9]*)_(arm64_v8a|x86_64)", platform)
    if not match or match[2] != abi.replace("-", "_") or not 1 <= int(match[1]) <= MIN_ANDROID_API:
        return False
    if interpreter == target and binary_abi in {target, "none", "abi3"}:
        return True
    if interpreter == "py3" and binary_abi == "none":
        return True
    # Older CPython stable-ABI wheels remain eligible. Free-threaded tags are not.
    if binary_abi == "abi3" and re.fullmatch(r"cp3[0-9]+", interpreter):
        return 2 <= int(interpreter[3:]) <= int(python.split(".")[1])
    return False


def audit_wheel(path: Path, python: str, abi: str) -> dict:
    match = WHEEL_NAME.fullmatch(path.name)
    require(match is not None, f"invalid wheel filename: {path.name}")
    assert match is not None
    parts = match.groupdict()
    distribution = canonical_name(parts["distribution"])
    tags = set(itertools.product(*(parts[key].split(".") for key in ("python", "abi", "platform"))))
    require(
        any(compatible_tag(tag, python, abi) for tag in tags),
        f"wheel tags do not support Python {python}, Android API 24, {abi}",
    )
    # A wheel must not claim host Linux or another architecture alongside Android.
    platforms = {tag[2] for tag in tags}
    require(
        all(
            platform == "any"
            or re.fullmatch(r"android_[0-9]+_" + re.escape(abi.replace("-", "_")), platform)
            for platform in platforms
        ),
        "wheel includes a non-target or host platform tag",
    )
    require(not ("any" in platforms and len(platforms) > 1), "wheel mixes any and native platforms")
    with path.open("rb") as source:
        source.seek(0, 2)
        require(source.tell() <= MAX_WHEEL_BYTES, "wheel exceeds compressed size limit")
        source.seek(0)
        payload = source.read()
    require(len(payload) <= MAX_WHEEL_BYTES, "wheel exceeds compressed size limit")
    files = read_archive(path, payload)
    dist_info = f"{parts['distribution']}-{parts['version']}.dist-info"
    actual_info = {
        name.split("/")[0] for name in files if name.split("/")[0].endswith(".dist-info")
    }
    require(
        actual_info == {dist_info},
        "wheel must have exactly the filename-matched dist-info directory",
    )
    wheel_name = f"{dist_info}/WHEEL"
    require(wheel_name in files, "missing WHEEL metadata")
    metadata = BytesParser().parsebytes(files[wheel_name])
    require(metadata.get_all("Wheel-Version") == ["1.0"], "unsupported or duplicate Wheel-Version")
    pure = metadata.get_all("Root-Is-Purelib")
    require(pure in (["true"], ["false"]), "invalid or duplicate Root-Is-Purelib")
    declared = metadata.get_all("Tag", [])
    require(len(declared) == len(set(declared)), "duplicate WHEEL Tag")
    require(
        set(declared) == {"-".join(tag) for tag in tags}, "filename/WHEEL tags do not match exactly"
    )
    project_name = f"{dist_info}/METADATA"
    require(project_name in files, "missing METADATA")
    project = BytesParser().parsebytes(files[project_name])
    require(
        len(project.get_all("Name", [])) == 1 and canonical_name(project["Name"]) == distribution,
        "METADATA Name does not match filename",
    )
    require(
        project.get_all("Version") == [parts["version"]], "METADATA Version does not match filename"
    )
    verify_record(files, dist_info)
    libraries = []
    for name, data in files.items():
        if data.startswith(b"\x7fELF") or re.search(r"\.so(?:\.[0-9]+)*$", name):
            if ".cpython-" in name:
                cpu = "aarch64" if abi == "arm64-v8a" else "x86_64"
                suffix = f".cpython-{python.replace('.', '')}-{cpu}-linux-android.so"
                require(
                    name.endswith(suffix), f"native extension SOABI does not match target: {name}"
                )
            libraries.append(audit_elf(data, abi, name))
        else:
            require(
                not data.startswith(
                    (
                        b"MZ",
                        b"\xcf\xfa\xed\xfe",
                        b"\xfe\xed\xfa\xcf",
                        b"\xce\xfa\xed\xfe",
                        b"\xfe\xed\xfa\xce",
                    )
                ),
                f"host native binary in wheel: {name}",
            )
    if libraries:
        require("any" not in platforms and pure == ["false"], "native ELF mislabeled as pure wheel")
    if distribution in REQUIRED_EXTENSIONS:
        extension = REQUIRED_EXTENSIONS[distribution]
        require(
            any(
                re.fullmatch(
                    re.escape(extension) + r"(?:\.[A-Za-z0-9_-]+)*\.so", Path(item["path"]).name
                )
                and f"PyInit_{extension}" in item["python_initializers"]
                for item in libraries
            ),
            f"{distribution} must contain its native {extension} extension",
        )
    return {
        "filename": path.name,
        "package": distribution,
        "version": parts["version"],
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "tags": sorted("-".join(tag) for tag in tags),
        "file_count": len(files),
        "native_libraries": libraries,
    }


def audit_directory(
    directory: Path, python: str, abi: str, package_filter: set[str] | None = None
) -> dict:
    require(python in {"3.13", "3.14"} and abi in MACHINES, "unsupported target")
    require(directory.is_dir(), f"wheel directory does not exist: {directory}")
    wheels, errors = [], []
    for path in sorted(directory.glob("*.whl")):
        match = WHEEL_NAME.fullmatch(path.name)
        if package_filter and match and canonical_name(match["distribution"]) not in package_filter:
            continue
        try:
            wheels.append(audit_wheel(path, python, abi))
        except (AuditError, OSError, zipfile.BadZipFile, RuntimeError) as error:
            errors.append({"wheel": path.name, "error": str(error)})
    if not wheels and not errors:
        errors.append({"error": "no selected wheels found"})
    packages = [wheel["package"] for wheel in wheels]
    for package in sorted(set(packages)):
        if packages.count(package) > 1:
            errors.append({"error": f"multiple selected wheels for package: {package}"})
    for package in sorted((package_filter or set()) - set(packages)):
        errors.append({"error": f"required package was not successfully audited: {package}"})
    provided: dict[str, str] = {}
    for wheel in wheels:
        for library in wheel["native_libraries"]:
            for name in {Path(library["path"]).name, library["soname"]} - {None}:
                if name in provided and provided[name] != library["sha256"]:
                    errors.append(
                        {
                            "wheel": wheel["filename"],
                            "error": f"conflicting bundled library: {name}",
                        }
                    )
                provided[name] = library["sha256"]
    allowed = ANDROID_LIBRARIES | {f"libpython{python}.so"} | set(provided)
    for wheel in wheels:
        for library in wheel["native_libraries"]:
            unresolved = sorted(set(library["needed"]) - allowed)
            if unresolved:
                errors.append(
                    {
                        "wheel": wheel["filename"],
                        "path": library["path"],
                        "error": f"unresolved or non-Android DT_NEEDED: {', '.join(unresolved)}",
                    }
                )
    return {
        "schema": 1,
        "passed": not errors,
        "python": python,
        "abi": abi,
        "minimum_android_api": MIN_ANDROID_API,
        "required_load_alignment": PAGE_SIZE,
        "scope": (
            "static wheel integrity and ELF audit only; "
            "Android runtime tests are a separate gate"
        ),
        "runtime_tested": False,
        "wheels": wheels,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--python", choices=("3.13", "3.14"), required=True)
    parser.add_argument("--abi", choices=tuple(MACHINES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--package-filter", nargs="+", help="package names, separated by spaces or commas"
    )
    args = parser.parse_args(argv)
    selected = {
        canonical_name(name)
        for value in (args.package_filter or [])
        for name in value.split(",")
        if name
    }
    try:
        report = audit_directory(args.wheel_dir, args.python, args.abi, selected or None)
    except (AuditError, OSError) as error:
        report = {
            "schema": 1,
            "passed": False,
            "runtime_tested": False,
            "errors": [{"error": str(error)}],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wheel static audit {'passed' if report['passed'] else 'FAILED'}: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
