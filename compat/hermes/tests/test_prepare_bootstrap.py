"""Adversarial bootstrap binding tests: no native runtime, network or Gradle needed."""

import base64
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from compat.hermes import prepare_bootstrap as bootstrap
from compat.hermes import build_bootstrap


PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


def make_pyc(source, filename, *, timestamp=123, flags=0):
    code = compile(source, filename, "exec", dont_inherit=True, optimize=0)
    rest = (importlib.util.source_hash(source) if flags & 1
            else struct.pack("<II", timestamp, len(source)))
    return importlib.util.MAGIC_NUMBER + struct.pack("<I", flags) + rest + marshal.dumps(code)


class PrepareBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.sources = self.repo / bootstrap.RUNTIME
        for name in bootstrap.INPUT_FILES:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("17.0.1\n" if name == "VERSION.txt" else "source input\n")
        self.source_bytes = {
            bootstrap.IMPORTER: b"def find_module(name):\n    return ('fixed', name)\n",
            "java/__init__.py": b"NAME = 'java'\n",
            "java/config.py": b"def nested():\n    return lambda: frozenset({1, 2})\n",
            "setuptools-68.2.2.dist-info/LICENSE": b"original upstream license\n",
            "setuptools-68.2.2.dist-info/METADATA": b"Name: setuptools\nVersion: 68.2.2\n",
            "setuptools-68.2.2.dist-info/entry_points.txt": b"[console_scripts]\n",
        }
        for name, data in self.source_bytes.items():
            path = self.sources / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.sources / "java/not-selected.pyx").write_text("cdef int value\n")
        self.archive = self.root / f"bootstrap-{PYTHON}.imy"
        self.output = self.root / "output"

    def archive_bytes(self, *, replacements=None, extra=(), omit=(), timestamp=123, flags=0):
        entries = {}
        for name, source in self.source_bytes.items():
            archived = name + "c" if name.endswith(".py") else name
            if archived not in omit:
                entries[archived] = (make_pyc(source, "./" + name, timestamp=timestamp, flags=flags)
                                     if name.endswith(".py") else source)
        entries.update(replacements or {})
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in [*entries.items(), *extra]:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, data)
        return stream.getvalue()

    def stage(self, data=None):
        self.archive.write_bytes(data if data is not None else self.archive_bytes())
        return bootstrap.stage(self.archive, PYTHON, self.output, repo_root=self.repo)

    def validate(self):
        return bootstrap.validate_receipt(self.output / "receipt.json", repo_root=self.repo,
                                          expected_python=PYTHON)

    def test_stage_preserves_exact_archive_and_binds_all_selected_sources(self):
        data = self.archive_bytes()
        result = self.stage(data)
        self.assertEqual((self.output / result["archive_path"]).read_bytes(), data)
        self.assertEqual(len(result["source_inventory"]), len(self.source_bytes))
        self.assertEqual(len(result["bytecode_inventory"]), 3)
        self.assertEqual(result["version"], "17.0.1")
        self.assertEqual(result["pyc_magic_hex"], importlib.util.MAGIC_NUMBER.hex())
        self.assertEqual(self.validate()["archive_sha256"], bootstrap.sha256(data))
        self.assertTrue((Path(result["maven_repo_path"]) / "com/chaquo/python/runtime/bootstrap"
                         / "17.0.1" / f"bootstrap-17.0.1-{PYTHON}.imy").is_file())

    def test_timestamp_headers_and_hash_headers_keep_same_compiled_code_identity(self):
        inspected = [bootstrap.inspect_archive(data, PYTHON, repo_root=self.repo) for data in (
            self.archive_bytes(timestamp=1), self.archive_bytes(timestamp=456),
            self.archive_bytes(flags=3),
        )]
        self.assertEqual(len({item["archive_sha256"] for item in inspected}), 3)
        self.assertEqual(len({item["importer_code_sha256"] for item in inspected}), 1)

    def test_source_builder_matches_upstream_compilation_and_license_selection(self):
        data = build_bootstrap.archive_bytes(PYTHON, repo_root=self.repo)
        built = bootstrap.inspect_archive(data, PYTHON, repo_root=self.repo)
        upstream = bootstrap.inspect_archive(self.archive_bytes(flags=3), PYTHON, repo_root=self.repo)
        self.assertEqual(built["source_inventory"], upstream["source_inventory"])
        self.assertEqual(
            [(row["source_path"], row["code_sha256"]) for row in built["bytecode_inventory"]],
            [(row["source_path"], row["code_sha256"]) for row in upstream["bytecode_inventory"]],
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(entry.date_time, (1980, 2, 1, 0, 0, 0))
        self.archive.write_bytes(self.archive_bytes(flags=3))
        result = build_bootstrap.build(PYTHON, self.output, repo_root=self.repo,
                                       compare_upstream=self.archive)
        self.assertEqual(self.validate()["archive_sha256"], bootstrap.sha256(data))
        self.assertEqual(result["archive_sha256"], bootstrap.sha256(data))
        with self.assertRaisesRegex(ValueError, "new directory"):
            build_bootstrap.build(PYTHON, self.output, repo_root=self.repo)

    def test_source_builder_ignores_checkout_paths_and_file_mtimes(self):
        import shutil
        other = self.root / "another-checkout"
        shutil.copytree(self.repo, other)
        for source in (other / bootstrap.RUNTIME).rglob("*"):
            if source.is_file():
                os.utime(source, (123, 123))
        self.assertEqual(build_bootstrap.archive_bytes(PYTHON, repo_root=self.repo),
                         build_bootstrap.archive_bytes(PYTHON, repo_root=other))

    def test_source_builder_supports_compiler_slice_constants(self):
        # CPython 3.14 folds constant slices into co_consts; marshal formats
        # before 5 cannot serialize them. Compile on the actual host minor.
        source = b"def sliced(value):\n    return value[1:4]\n"
        path = self.sources / "java/sliced.py"
        path.write_bytes(source)
        data = build_bootstrap.archive_bytes(PYTHON, repo_root=self.repo)
        report = bootstrap.inspect_archive(data, PYTHON, repo_root=self.repo)
        self.assertIn("java/sliced.py", [row["source_path"] for row in report["bytecode_inventory"]])

    def test_source_inventory_order_does_not_depend_on_windows_path_comparison(self):
        class CaseInsensitivePath(type(Path())):
            def __lt__(self, other):
                return str(self).casefold() < str(other).casefold()

        entries = [CaseInsensitivePath(path) for path in self.sources.rglob("*")]
        with patch.object(Path, "rglob", return_value=iter(entries)):
            context, _ = bootstrap.source_inputs(self.repo)
        names = [item["path"] for item in context["source_inventory"]]
        self.assertEqual(names, sorted(self.source_bytes))
        ordinary, _ = bootstrap.source_inputs(self.repo)
        self.assertEqual(context["source_inventory_sha256"], ordinary["source_inventory_sha256"])
        self.assertEqual(context["input_sha256"], ordinary["input_sha256"])

    def test_old_stock_importer_bytecode_cannot_pass_source_validation(self):
        old = b"def find_module(name):\n    return ('stock', name)\n"
        data = self.archive_bytes(replacements={
            bootstrap.IMPORTER + "c": make_pyc(old, "./" + bootstrap.IMPORTER),
        })
        with self.assertRaisesRegex(bootstrap.BootstrapError, "current fork source"):
            self.stage(data)
        self.assertFalse(self.output.exists())

    def test_all_pyc_modules_are_checked_not_only_importer(self):
        wrong = b"NAME = 'evil'\n"
        data = self.archive_bytes(replacements={
            "java/__init__.pyc": make_pyc(wrong, "./java/__init__.py"),
        })
        with self.assertRaisesRegex(bootstrap.BootstrapError, "current fork source"):
            self.stage(data)

    def test_magic_hash_source_length_flags_and_trailing_data_are_checked(self):
        source = self.source_bytes[bootstrap.IMPORTER]
        good = make_pyc(source, "./" + bootstrap.IMPORTER)
        invalid = [b"BAD!" + good[4:], good[:4] + struct.pack("<I", 2) + good[8:],
                   good[:12] + struct.pack("<I", 999) + good[16:], good + b"extra",
                   make_pyc(source, "./" + bootstrap.IMPORTER, flags=3)[:8]
                   + b"BAD-HASH" + good[16:]]
        for payload in invalid:
            with self.subTest(payload=payload[:16]):
                with self.assertRaises(bootstrap.BootstrapError):
                    self.stage(self.archive_bytes(replacements={bootstrap.IMPORTER + "c": payload}))

    def test_absolute_parent_and_unexpected_code_filenames_are_rejected(self):
        for filename in ("/home/runner/importer.py", "C:\\checkout\\importer.py",
                         "../java/android/importer.py", "other.py"):
            with self.subTest(filename=filename):
                payload = make_pyc(self.source_bytes[bootstrap.IMPORTER], filename)
                with self.assertRaisesRegex(bootstrap.BootstrapError, "source filename"):
                    self.stage(self.archive_bytes(replacements={bootstrap.IMPORTER + "c": payload}))

    def test_archive_inventory_and_license_are_exact(self):
        cases = (
            self.archive_bytes(extra=[("extra.pyc", b"unexpected")]),
            self.archive_bytes(omit=["java/config.pyc"]),
            self.archive_bytes(replacements={
                "setuptools-68.2.2.dist-info/LICENSE": b"changed license\n",
            }),
        )
        for data in cases:
            with self.assertRaises(bootstrap.BootstrapError):
                self.stage(data)

    def test_traversal_duplicates_and_link_zip_members_are_rejected(self):
        link = zipfile.ZipInfo("java/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for member in ("../escape", "/absolute", "java\\escape", "java/config.pyc", link):
            with self.subTest(member=member):
                with self.assertRaises(bootstrap.BootstrapError):
                    self.stage(self.archive_bytes(extra=[(member, b"bad")]))
        self.assertFalse((self.root / "escape").exists())

    def test_receipt_rejects_current_source_lock_and_version_drift(self):
        self.stage()
        for relative in (bootstrap.RUNTIME / bootstrap.IMPORTER, Path("VERSION.txt"),
                         Path("compat/hermes/sources.lock.json")):
            path = self.repo / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"\n")
                with self.assertRaises(bootstrap.BootstrapError):
                    self.validate()
            finally:
                path.write_bytes(original)

    def test_receipt_pom_and_archive_tampering_are_rejected(self):
        result = self.stage()
        paths = [self.output / "receipt.json", self.output / result["pom_path"],
                 self.output / result["archive_path"]]
        for path in paths:
            original = path.read_bytes()
            try:
                if path.name == "receipt.json":
                    receipt = json.loads(original)
                    receipt["importer_source_sha256"] = "0" * 64
                    path.write_text(json.dumps(receipt))
                else:
                    path.write_bytes(original + b"changed")
                with self.assertRaises(bootstrap.BootstrapError):
                    self.validate()
            finally:
                path.write_bytes(original)

    def test_existing_output_and_mismatched_host_are_rejected(self):
        self.output.mkdir()
        sentinel = self.output / "user.txt"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "new directory"):
            self.stage()
        self.assertEqual(sentinel.read_text(), "keep")
        other = "3.13" if PYTHON == "3.14" else "3.14"
        with self.assertRaisesRegex(bootstrap.BootstrapError, "Use CPython"):
            bootstrap.inspect_archive(self.archive_bytes(), other, repo_root=self.repo)

    def test_apk_must_embed_exact_source_built_bootstrap(self):
        result = self.stage()
        apk = self.root / "app.apk"
        bootstrap_bytes = (self.output / result["archive_path"]).read_bytes()
        with zipfile.ZipFile(apk, "w") as output:
            output.writestr(bootstrap.APK_ENTRY, bootstrap_bytes)
        verified = bootstrap.verify_apk(apk, self.output / "receipt.json", python=PYTHON,
                                        repo_root=self.repo)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["archive_sha256"], result["archive_sha256"])
        changed_archive = bytearray(bootstrap_bytes)
        changed_archive[10] ^= 1  # Same-size ZIP with a different local-header timestamp.
        with zipfile.ZipFile(apk, "w") as output:
            output.writestr(bootstrap.APK_ENTRY, changed_archive)
        with self.assertRaisesRegex(bootstrap.BootstrapError, "stock fallback rejected"):
            bootstrap.verify_apk(apk, self.output / "receipt.json", python=PYTHON,
                                 repo_root=self.repo)

    def test_duplicate_and_missing_apk_bootstraps_are_rejected(self):
        result = self.stage()
        payload = (self.output / result["archive_path"]).read_bytes()
        apk = self.root / "app.apk"
        for count in (0, 2):
            with zipfile.ZipFile(apk, "w") as output, warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                for _ in range(count):
                    output.writestr(bootstrap.APK_ENTRY, payload)
            with self.assertRaisesRegex(bootstrap.BootstrapError, "exactly one"):
                bootstrap.verify_apk(apk, self.output / "receipt.json", python=PYTHON,
                                     repo_root=self.repo)

    def test_archive_expansion_limit_is_fail_closed(self):
        with patch.object(bootstrap, "MAX_EXPANDED_BYTES", 1):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "expanded byte limit"):
                self.stage()

    def test_canonical_code_is_independent_of_compiler_hash_seed(self):
        source = b"def contains(value):\n    return value in {'alpha', 'beta', 'gamma'}\n"
        script = (
            "import base64,importlib.util,marshal,struct,sys;"
            "source=bytes.fromhex(sys.argv[1]);"
            "code=compile(source,'./java/config.py','exec',dont_inherit=True,optimize=0);"
            "data=importlib.util.MAGIC_NUMBER+struct.pack('<I',3)"
            "+importlib.util.source_hash(source)+marshal.dumps(code);"
            "print(base64.b64encode(data).decode())"
        )
        fingerprints = []
        for seed in ("0", "17"):
            result = subprocess.run(
                [sys.executable, "-c", script, source.hex()],
                env={**os.environ, "PYTHONHASHSEED": seed},
                check=True, capture_output=True, text=True, timeout=30,
            )
            record = bootstrap.validate_pyc(
                base64.b64decode(result.stdout), source, "java/config.py"
            )
            fingerprints.append(record["code_sha256"])
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_current_complete_runtime_tree_compiles_and_has_canonical_code(self):
        # Exercise every real vendored module, not only the tiny archive fixtures.
        # This is a compiler/checker test, not an upstream Gradle archive build.
        context, contents = bootstrap.source_inputs(bootstrap.ROOT)
        modules = [item for item in context["source_inventory"] if item["kind"] == "python"]
        self.assertGreater(len(modules), 10)
        for item in modules:
            with self.subTest(module=item["path"]):
                source = contents[item["path"]]
                payload = make_pyc(source, "./" + item["path"], flags=3)
                bootstrap.validate_pyc(payload, source, item["path"])

    def test_upstream_compileall_invocation_produces_accepted_relative_hash_pyc(self):
        subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "-b", "."],
            cwd=self.sources,
            env={**os.environ, "SOURCE_DATE_EPOCH": "1754006400", "PYTHONHASHSEED": "0"},
            check=True, capture_output=True, timeout=30,
        )
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in self.source_bytes:
                archived = name + "c" if name.endswith(".py") else name
                payload = (self.sources / archived).read_bytes()
                if name.endswith(".py"):
                    self.assertEqual(int.from_bytes(payload[4:8], "little"), 3)
                archive.writestr(archived, payload)
        result = bootstrap.inspect_archive(stream.getvalue(), PYTHON, repo_root=self.repo)
        self.assertTrue(all(item["co_filename"].startswith(("./", ".\\"))
                            for item in result["bytecode_inventory"]))


if __name__ == "__main__":
    unittest.main()
