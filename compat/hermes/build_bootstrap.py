#!/usr/bin/env python3
"""Compile the source-selected Python bootstrap without the Java build toolchain.

Uses the same Python/dist-info selection and unoptimized relative code filenames
as runtime:zipPython-*. ZIP storage and hash-based PYC headers are deterministic.
The existing independent validator checks every code object and license before
staging. This builds Python only, not the Java/JNI runtime or Gradle plugin.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import stat
import tempfile
import zipfile

try:
    from . import prepare_bootstrap as verifier
except ImportError:
    import prepare_bootstrap as verifier


def archive_bytes(python, *, repo_root=verifier.ROOT):
    verifier.host_python(python)
    context, contents = verifier.source_inputs(repo_root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for item in sorted(context["source_inventory"], key=lambda item: item["archive_path"]):
            source = contents[item["path"]]
            payload = source
            if item["kind"] == "python":
                code = compile(source, "./" + item["path"], "exec", dont_inherit=True, optimize=0)
                payload = (importlib.util.MAGIC_NUMBER + (3).to_bytes(4, "little")
                           + importlib.util.source_hash(source) + marshal.dumps(code, 2))
            entry = zipfile.ZipInfo(item["archive_path"], (1980, 2, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, payload)
    result = buffer.getvalue()
    verifier.inspect_archive(result, python, repo_root=repo_root)
    return result


def build(python, output, *, repo_root=verifier.ROOT, compare_upstream=None):
    verifier.require(not output.exists(), "Output must be a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-source-", dir=output.parent) as temporary:
        archive = Path(temporary) / "bootstrap.imy"
        data = archive_bytes(python, repo_root=repo_root)
        if compare_upstream is not None:
            upstream = verifier.inspect_archive(
                verifier.read_file(compare_upstream, verifier.MAX_ARCHIVE_BYTES),
                python, repo_root=repo_root)
            built = verifier.inspect_archive(data, python, repo_root=repo_root)
            def code_inventory(report):
                return [(row["source_path"], row["code_sha256"])
                        for row in report["bytecode_inventory"]]
            verifier.require(code_inventory(upstream) == code_inventory(built),
                             "Source bootstrap differs from upstream-compiled code")
        archive.write_bytes(data)
        return verifier.stage(archive, python, output, repo_root=repo_root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", choices=("3.13", "3.14"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-upstream", type=Path,
                        help="Require identical code to an independently Gradle-built archive")
    args = parser.parse_args()
    if os.environ.get("PYTHONHASHSEED") != "0":
        parser.error("Start with PYTHONHASHSEED=0 for deterministic compiled constants")
    print(json.dumps(build(args.python, args.output, compare_upstream=args.compare_upstream),
                     sort_keys=True))


if __name__ == "__main__":
    main()
