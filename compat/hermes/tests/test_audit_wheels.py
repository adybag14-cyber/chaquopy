"""Adversarial fixtures; these do not pretend to be runnable Android binaries."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "audit_wheels.py"
SPEC = importlib.util.spec_from_file_location("hermes_audit_wheels", MODULE_PATH)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def elf(
    *,
    machine=183,
    align=16384,
    vaddr=0,
    needed=("libc.so", "libpython3.14.so"),
    soname=None,
    initializer="PyInit_jiter",
    terminated=True,
):
    """Minimal ELF64 ET_DYN fixture with PT_LOAD, PT_DYNAMIC, and DYNSYM."""
    data = bytearray(1024)
    data[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, machine, 1, 0, 64, 768, 0, 64, 56, 2, 64, 3, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, vaddr, 0, len(data), len(data), align)
    strings = bytearray(b"\x00")

    def string(value):
        offset = len(strings)
        strings.extend(value.encode("ascii") + b"\x00")
        return offset

    entries = [(1, string(value)) for value in needed]
    if soname:
        entries.append((14, string(soname)))
    symbol_offset = string(initializer) if initializer else 0
    entries += [(5, vaddr + 512), (10, len(strings)), (6, vaddr + 640), (11, 24)]
    if terminated:
        entries += [(0, 0)]
    struct.pack_into(
        "<IIQQQQQQ", data, 120, 2, 6, 256, vaddr + 256, 0, len(entries) * 16, len(entries) * 16, 8
    )
    for index, entry in enumerate(entries):
        struct.pack_into("<qQ", data, 256 + index * 16, *entry)
    data[512 : 512 + len(strings)] = strings
    struct.pack_into("<IIQQQQIIQQ", data, 832, 0, 3, 0, vaddr + 512, 512, len(strings), 0, 0, 1, 0)
    struct.pack_into("<IIQQQQIIQQ", data, 896, 0, 11, 0, vaddr + 640, 640, 48, 1, 0, 8, 24)
    if initializer:
        struct.pack_into("<IBBHQQ", data, 664, symbol_offset, 0x12, 0, 1, 184, 1)
    return bytes(data)


def wheel_files(
    name="jiter",
    version="0.16.0",
    tag="cp314-cp314-android_24_arm64_v8a",
    *,
    data=None,
    pure=False,
    metadata_tag=None,
):
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{dist}/WHEEL": (
            "Wheel-Version: 1.0\n"
            f"Root-Is-Purelib: {str(pure).lower()}\n"
            f"Tag: {metadata_tag or tag}\n"
        ).encode(),
        f"{dist}/METADATA": f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n".encode(),
    }
    files.update(
        data if data is not None else {"jiter/jiter.cpython-314-aarch64-linux-android.so": elf()}
    )
    return files, dist, f"{name}-{version}-{tag}.whl"


def with_record(files, dist):
    files = dict(files)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + digest, str(len(data))))
    writer.writerow((f"{dist}/RECORD", "", ""))
    files[f"{dist}/RECORD"] = output.getvalue().encode()
    return files


class WheelAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_wheel(self, *, transform=None, extra=None, **kwargs):
        files, dist, filename = wheel_files(**kwargs)
        files = with_record(files, dist)
        if transform:
            transform(files, dist)
        path = self.root / filename
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            if extra:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    extra(archive)
        return path

    def audit(self, **kwargs):
        return audit.audit_directory(
            self.root, kwargs.pop("python", "3.14"), kwargs.pop("abi", "arm64-v8a"), **kwargs
        )

    def rejected(self, text, **kwargs):
        report = self.audit(**kwargs)
        self.assertFalse(report["passed"], report)
        self.assertIn(text, str(report["errors"]))

    def test_valid_native_wheel_has_static_not_runtime_result(self):
        self.write_wheel()
        report = self.audit()
        self.assertTrue(report["passed"], report)
        self.assertFalse(report["runtime_tested"])
        library = report["wheels"][0]["native_libraries"][0]
        self.assertEqual(library["python_initializers"], ["PyInit_jiter"])
        self.assertEqual(library["minimum_load_alignment"], 16384)

    def test_python313_x86_64(self):
        self.write_wheel(
            tag="cp313-cp313-android_24_x86_64",
            data={"jiter/jiter.so": elf(machine=62, needed=("libpython3.13.so",))},
        )
        self.assertTrue(self.audit(python="3.13", abi="x86_64")["passed"])

    def test_pure_wheel(self):
        self.write_wheel(
            name="msgpack",
            version="1.2.2",
            tag="py3-none-any",
            pure=True,
            data={"msgpack/__init__.py": b"# Pure fallback\n"},
        )
        self.assertTrue(self.audit()["passed"])

    def test_abi3_wheel(self):
        self.write_wheel(tag="cp39-abi3-android_24_arm64_v8a")
        self.assertTrue(self.audit()["passed"])

    def test_build_tag(self):
        path = self.write_wheel()
        path.rename(path.with_name(path.name.replace("-cp314-", "-1-cp314-", 1)))
        self.assertTrue(self.audit()["passed"])

    def test_wrong_python(self):
        self.write_wheel()
        self.rejected("tags do not support", python="3.13")

    def test_wrong_architecture_tag(self):
        self.write_wheel(tag="cp314-cp314-android_24_x86_64")
        self.rejected("tags do not support")

    def test_host_linux_tag(self):
        self.write_wheel(tag="cp314-cp314-manylinux_2_28_aarch64")
        self.rejected("tags do not support")

    def test_api_too_new(self):
        self.write_wheel(tag="cp314-cp314-android_26_arm64_v8a")
        self.rejected("tags do not support")

    def test_wrong_elf_machine(self):
        self.write_wheel(data={"jiter/jiter.so": elf(machine=62)})
        self.rejected("ELF machine 62")

    def test_mismatched_wheel_metadata_tag(self):
        self.write_wheel(metadata_tag="cp313-cp313-android_24_arm64_v8a")
        self.rejected("filename/WHEEL tags")

    def test_mixed_host_android_tags(self):
        tag = "cp314-cp314-android_24_arm64_v8a.linux_aarch64"
        self.write_wheel(tag=tag)
        self.rejected("host platform tag")

    def test_native_marked_pure(self):
        self.write_wheel(pure=True)
        self.rejected("mislabeled as pure")

    def test_elf_hidden_in_pure_wheel(self):
        self.write_wheel(
            name="helper", tag="py3-none-any", pure=True, data={"helper/payload.dat": elf()}
        )
        self.rejected("mislabeled as pure")

    def test_required_native_missing(self):
        self.write_wheel(tag="py3-none-any", pure=True, data={"jiter/__init__.py": b"pass\n"})
        self.rejected("must contain its native")

    def test_required_initializer_missing(self):
        self.write_wheel(data={"jiter/jiter.so": elf(initializer=None)})
        self.rejected("must contain its native")

    def test_pydantic_core_initializer(self):
        self.write_wheel(
            name="pydantic_core",
            version="2.46.5",
            data={"pydantic_core/_pydantic_core.so": elf(initializer="PyInit__pydantic_core")},
        )
        self.assertTrue(self.audit(package_filter={"pydantic-core"})["passed"])

    def test_4kb_alignment(self):
        self.write_wheel(data={"jiter/jiter.so": elf(align=4096)})
        self.rejected("PT_LOAD alignment")

    def test_non_power_two_alignment(self):
        self.write_wheel(data={"jiter/jiter.so": elf(align=24576)})
        self.rejected("PT_LOAD alignment")

    def test_incongruent_segment(self):
        self.write_wheel(data={"jiter/jiter.so": elf(vaddr=4096)})
        self.rejected("not congruent")

    def test_truncated_elf(self):
        self.write_wheel(data={"jiter/jiter.so": elf()[:600]})
        self.rejected("outside file")

    def test_unterminated_dynamic_table(self):
        self.write_wheel(data={"jiter/jiter.so": elf(terminated=False)})
        self.rejected("dynamic table is not terminated")

    def test_glibc_needed(self):
        self.write_wheel(data={"jiter/jiter.so": elf(needed=("libc.so.6",))})
        self.rejected("non-Android DT_NEEDED")

    def test_wrong_python_library_needed(self):
        self.write_wheel(data={"jiter/jiter.so": elf(needed=("libpython3.13.so",))})
        self.rejected("libpython3.13.so")

    def test_unbundled_cxx_runtime(self):
        self.write_wheel(data={"jiter/jiter.so": elf(needed=("libc++_shared.so",))})
        self.rejected("libc++_shared.so")

    def test_bundled_soname_resolves(self):
        self.write_wheel(
            data={
                "jiter/jiter.so": elf(needed=("libcustom.so",)),
                "jiter.libs/libcustom-renamed.so": elf(soname="libcustom.so", initializer=None),
            }
        )
        self.assertTrue(self.audit()["passed"])

    def test_conflicting_soname(self):
        self.write_wheel(
            data={
                "jiter/jiter.so": elf(),
                "jiter.libs/one.so": elf(soname="shared.so", initializer=None),
                "jiter.libs/two.so": elf(soname="shared.so", initializer="PyInit_fake"),
            }
        )
        self.rejected("conflicting bundled library")

    def test_needed_path_is_unsafe(self):
        self.write_wheel(data={"jiter/jiter.so": elf(needed=("../libevil.so",))})
        self.rejected("unsafe ELF library name")

    def test_host_pe_binary(self):
        self.write_wheel(data={"jiter/jiter.so": elf(), "helper.exe": b"MZ" + bytes(100)})
        self.rejected("host native binary")

    def test_record_tampering(self):
        def modify(files, dist):
            files[f"{dist}/METADATA"] += b"Summary: tampered\n"

        self.write_wheel(transform=modify)
        self.rejected("RECORD size mismatch")

    def test_record_same_size_hash_tampering(self):
        def modify(files, dist):
            name = next(name for name in files if name.endswith(".so"))
            files[name] = files[name][:-1] + b"x"

        self.write_wheel(transform=modify)
        self.rejected("RECORD hash mismatch")

    def test_record_missing_file(self):
        self.write_wheel(
            transform=lambda files, dist: files.pop(next(n for n in files if n.endswith(".so")))
        )
        self.rejected("RECORD references missing file")

    def test_unrecorded_member(self):
        self.write_wheel(extra=lambda archive: archive.writestr("unrecorded.py", b"pass\n"))
        self.rejected("exact wheel file set")

    def test_duplicate_zip_member(self):
        self.write_wheel(
            extra=lambda archive: archive.writestr(
                "jiter/jiter.cpython-314-aarch64-linux-android.so", elf()
            )
        )
        self.rejected("duplicate ZIP path")

    def test_duplicate_record_row(self):
        def modify(files, dist):
            path = f"{dist}/RECORD"
            files[path] += files[path].splitlines(keepends=True)[0]

        self.write_wheel(transform=modify)
        self.rejected("duplicate RECORD path")

    def test_record_weak_hash(self):
        def modify(files, dist):
            path = f"{dist}/RECORD"
            files[path] = files[path].replace(b"sha256=", b"sha1=", 1)

        self.write_wheel(transform=modify)
        self.rejected("strong hash")

    def test_traversal_paths(self):
        for name in (
            "../evil.py",
            "/absolute.py",
            "a/../evil.py",
            "a//evil.py",
            "a\\evil.py",
            "C:/evil.py",
        ):
            with self.subTest(name=name):

                def extra(archive):
                    # ZipInfo's constructor normalizes backslashes on Windows;
                    # bypass that normalization to model a hostile input ZIP.
                    entry = zipfile.ZipInfo("placeholder")
                    entry.filename = name
                    archive.writestr(entry, b"x")

                self.write_wheel(extra=extra)
                self.rejected("path")

    def test_nul_path_is_rejected(self):
        with self.assertRaisesRegex(audit.AuditError, "NUL"):
            audit.canonical_path("a\x00b")

    def test_duplicate_wheel_tag(self):
        def modify(files, dist):
            path = f"{dist}/WHEEL"
            files[path] += b"Tag: cp314-cp314-android_24_arm64_v8a\n"

        self.write_wheel(transform=modify)
        self.rejected("duplicate WHEEL Tag")

    def test_metadata_name_mismatch(self):
        def modify(files, dist):
            path = f"{dist}/METADATA"
            files[path] = files[path].replace(b"Name: jiter", b"Name: other")

        self.write_wheel(transform=modify)
        self.rejected("METADATA Name")

    def test_wrong_elf_class(self):
        data = bytearray(elf())
        data[4] = 1
        self.write_wheel(data={"jiter/jiter.so": bytes(data)})
        self.rejected("64-bit little-endian")

    def test_wrong_extension_soabi(self):
        self.write_wheel(data={"jiter/jiter.cpython-313-aarch64-linux-android.so": elf()})
        self.rejected("SOABI does not match target")

    def test_fake_symbol_section_not_used_by_loader(self):
        data = bytearray(elf())
        struct.pack_into("<Q", data, 256 + 4 * 16 + 8, 720)
        self.write_wheel(data={"jiter/jiter.so": bytes(data)})
        self.rejected("do not match DT_SYMTAB")

    def test_dynamic_table_not_loaded(self):
        data = bytearray(elf())
        struct.pack_into("<Q", data, 120 + 16, 257)
        self.write_wheel(data={"jiter/jiter.so": bytes(data)})
        self.rejected("not mapped by PT_LOAD")

    def test_string_table_not_loaded(self):
        data = bytearray(elf())
        struct.pack_into("<Q", data, 256 + 2 * 16 + 8, 5000)
        self.write_wheel(data={"jiter/jiter.so": bytes(data)})
        self.rejected("string table is not file-backed")

    def test_shared_library_with_no_elf(self):
        self.write_wheel(data={"jiter/jiter.so": b"not ELF"})
        self.rejected("invalid ELF")

    def test_oversized_declared_archive(self):
        self.write_wheel()
        previous = audit.MAX_WHEEL_BYTES
        try:
            audit.MAX_WHEEL_BYTES = 1
            self.rejected("size limit")
        finally:
            audit.MAX_WHEEL_BYTES = previous

    def test_signature_record_exception(self):
        self.write_wheel(
            extra=lambda archive: archive.writestr(
                "jiter-0.16.0.dist-info/RECORD.jws", b"signature"
            )
        )
        self.assertTrue(self.audit()["passed"])

    def test_symlink(self):
        def extra(archive):
            entry = zipfile.ZipInfo("symlink")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "target")

        self.write_wheel(extra=extra)
        self.rejected("symlink or special")

    def test_file_directory_collision(self):
        self.write_wheel(data={"jiter": b"x", "jiter/jiter.so": elf()})
        self.rejected("file/directory collision")

    def test_second_dist_info(self):
        self.write_wheel(
            data={"jiter/jiter.so": elf(), "evil-1.dist-info/METADATA": b"Name: evil\n"}
        )
        self.rejected("filename-matched dist-info")

    def test_empty_directory_fails(self):
        self.rejected("no selected wheels")

    def test_package_filter_requires_presence(self):
        self.write_wheel()
        self.rejected("required package", package_filter={"jiter", "pydantic-core"})

    def test_package_filter_excludes_other_packages(self):
        self.write_wheel()
        self.write_wheel(name="other", tag="cp314-cp314-linux_aarch64")
        self.assertTrue(self.audit(package_filter={"jiter"})["passed"])

    def test_duplicate_package_versions(self):
        self.write_wheel()
        self.write_wheel(version="0.15.0")
        self.rejected("multiple selected wheels")

    def test_cli_writes_failure_json_and_nonzero_exit(self):
        self.write_wheel(data={"jiter/jiter.so": elf(align=4096)})
        output = self.root / "reports" / "audit.json"
        code = audit.main(
            [
                "--wheel-dir",
                str(self.root),
                "--python",
                "3.14",
                "--abi",
                "arm64-v8a",
                "--output",
                str(output),
                "--package-filter",
                "jiter,pydantic-core",
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.read_text())["passed"])


if __name__ == "__main__":
    unittest.main()
