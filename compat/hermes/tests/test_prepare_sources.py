"""Offline source-preparation regression coverage with tiny synthetic sdists."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from compat.hermes import prepare_sources as sources


class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dest = self.root / "prepared"
        self.lock = copy.deepcopy(sources.load_lock())

    def fixture(self, package="jiter", extras=(), *, requirement=None, version=None):
        entry = self.lock["packages"][package]
        source_root = entry["source_directory"]
        requirements = requirement or entry["build"].get(
            "original_requirement", "setuptools >= 78.1.1"
        )
        pyproject = (
            '[build-system]\nrequires = ["' + requirements + '"]\n'
            'build-backend = "' + entry["build"]["backend"] + '"\n'
            '[project]\nname = "' + package + '"\n'
        ).encode()
        files = [
            (source_root + "/PKG-INFO",
             f"Name: {package}\nVersion: {version or entry['version']}\n".encode()),
            (source_root + "/pyproject.toml", pyproject),
            (source_root + "/src/module.py", b"VALUE = 1\n"),
        ]
        archive = self.root / entry["filename"]
        with tarfile.open(archive, "w:gz") as output:
            for name, data in files:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
            for member, data in extras:
                if isinstance(member, str):
                    member = tarfile.TarInfo(member)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        entry["sha256"] = sources.sha256_file(archive)
        entry["bytes"] = archive.stat().st_size
        self.lock_path = self.root / "sources.lock.json"
        self.write_lock()
        return archive, pyproject

    def write_lock(self):
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")

    def prepare(self, package="jiter", archive=None):
        return sources.prepare_package(
            package, self.dest, lock_path=self.lock_path, archive_path=archive
        )

    def test_live_lock_targets_and_pins_are_explicit(self):
        self.assertEqual(self.lock["targets"], {
            "python": ["3.13", "3.14"], "android_api": 24, "abis": ["arm64-v8a", "x86_64"],
        })
        self.assertEqual(
            self.lock["packages"]["pydantic_core"]["paired_requirement"], "pydantic==2.14.3"
        )
        self.assertEqual(
            self.lock["packages"]["msgpack"]["build"]["environment"], {"MSGPACK_PUREPYTHON": "1"}
        )

    def test_maturin_pin_has_exact_before_and_after_provenance(self):
        for package in ("jiter", "pydantic_core"):
            with self.subTest(package=package):
                archive, original = self.fixture(package)
                receipt = self.prepare(package, archive)
                source = Path(receipt["source_dir"])
                changed = (source / "pyproject.toml").read_bytes()
                self.assertIn(self.lock['packages'][package]['build']['pinned_requirement'].encode(), changed)
                self.assertNotEqual(changed, original)
                self.assertEqual(
                    receipt["patches"][0]["before_sha256"], hashlib.sha256(original).hexdigest()
                )
                self.assertEqual(
                    receipt["patches"][0]["after_sha256"], hashlib.sha256(changed).hexdigest()
                )
                self.assertEqual(json.loads(Path(receipt["provenance_path"]).read_text()), receipt)
                self.assertFalse(receipt["android_compatibility_verified"])

    def test_msgpack_uses_upstream_build_environment_without_retagging(self):
        archive, original = self.fixture("msgpack")
        receipt = self.prepare("msgpack", archive)
        self.assertEqual((Path(receipt["source_dir"]) / "pyproject.toml").read_bytes(), original)
        self.assertEqual(receipt["patches"], [])
        self.assertEqual(receipt["build_environment"], {"MSGPACK_PUREPYTHON": "1"})
        self.assertEqual(list(self.dest.rglob("*.whl")), [])

    def test_distribution_name_alias_uses_canonical_source_and_receipt(self):
        archive, _ = self.fixture("pydantic_core")
        receipt = self.prepare("pydantic-core", archive)
        self.assertEqual(receipt["package"], "pydantic_core")
        self.assertEqual(Path(receipt["source_dir"]).name, "pydantic_core-2.50.4")

    def test_hash_mismatch_is_rejected_before_extraction(self):
        archive, _ = self.fixture()
        self.lock["packages"]["jiter"]["sha256"] = "0" * 64
        self.write_lock()
        with patch.object(sources, "extract_archive") as extract:
            with self.assertRaisesRegex(sources.PreparationError, "SHA-256"):
                self.prepare(archive=archive)
            extract.assert_not_called()
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_size_mismatch_is_rejected(self):
        archive, _ = self.fixture()
        self.lock["packages"]["jiter"]["bytes"] += 1
        self.write_lock()
        with self.assertRaisesRegex(sources.PreparationError, "size"):
            self.prepare(archive=archive)

    def test_unsafe_and_unexpected_paths_are_rejected(self):
        for path in ("../escaped", "/absolute", "C:/escape", "other/file", "jiter-0.19.0/../escape",
                     "jiter-0.19.0\\escape", "jiter-0.19.0/./alias", "jiter-0.19.0/file:ads"):
            with self.subTest(path=path):
                archive, _ = self.fixture(extras=[(path, b"bad")])
                with self.assertRaises(sources.PreparationError):
                    self.prepare(archive=archive)
                self.assertEqual(list(self.dest.iterdir()), [])
        self.assertFalse((self.root / "escaped").exists())

    def test_links_and_special_files_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo("jiter-0.19.0/bad")
                member.type = kind
                member.linkname = "../outside"
                archive, _ = self.fixture(extras=[(member, b"")])
                with self.assertRaisesRegex(sources.PreparationError, "links/special"):
                    self.prepare(archive=archive)
                self.assertEqual(list(self.dest.iterdir()), [])

    def test_duplicate_casefolded_paths_are_rejected(self):
        archive, _ = self.fixture(extras=[("jiter-0.19.0/SRC/MODULE.py", b"bad")])
        with self.assertRaisesRegex(sources.PreparationError, "Duplicate"):
            self.prepare(archive=archive)

    def test_expansion_limit_is_checked_before_extraction(self):
        archive, _ = self.fixture()
        with patch.object(sources, "MAX_UNPACKED_BYTES", 1):
            with self.assertRaisesRegex(sources.PreparationError, "expanded byte"):
                self.prepare(archive=archive)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_existing_source_is_never_overwritten(self):
        archive, _ = self.fixture()
        receipt = self.prepare(archive=archive)
        source = Path(receipt["source_dir"])
        (source / "keep.txt").write_text("user work")
        with patch.object(sources, "download_archive") as download:
            with self.assertRaisesRegex(sources.PreparationError, "overwrite"):
                self.prepare()
            download.assert_not_called()
        self.assertEqual((source / "keep.txt").read_text(), "user work")

    def test_identity_and_unexpected_build_requirement_are_rejected(self):
        for options in ({"version": "9.9.9"}, {"requirement": "maturin>=2"}):
            with self.subTest(options=options):
                archive, _ = self.fixture(**options)
                with self.assertRaises(sources.PreparationError):
                    self.prepare(archive=archive)
                self.assertEqual(list(self.dest.iterdir()), [])

    def test_download_passes_bounded_timeout_and_verifies_content(self):
        archive, _ = self.fixture()
        entry = self.lock["packages"]["jiter"]
        response = io.BytesIO(archive.read_bytes())
        response.geturl = lambda: entry["url"]
        with patch.object(sources, "urlopen", return_value=response) as fetch:
            receipt = sources.prepare_package(
                "jiter", self.dest, lock_path=self.lock_path, timeout=17
            )
        self.assertEqual(fetch.call_args.kwargs["timeout"], 17)
        self.assertEqual(fetch.call_args.args[0].full_url, entry["url"])
        self.assertTrue(Path(receipt["source_dir"]).is_dir())

    def test_download_rejects_redirects_and_oversized_responses(self):
        archive, _ = self.fixture()
        for redirect, content in (
                ("http://files.pythonhosted.org/packages/bad", b""),
                (self.lock["packages"]["jiter"]["url"], archive.read_bytes() + b"x")):
            with self.subTest(redirect=redirect):
                response = io.BytesIO(content)
                response.geturl = lambda: redirect
                with patch.object(sources, "urlopen", return_value=response):
                    with self.assertRaises(sources.PreparationError):
                        self.prepare()
                self.assertEqual(list(self.dest.iterdir()), [])

    def test_invalid_timeout_fails_without_network(self):
        self.fixture()
        with patch.object(sources, "urlopen") as fetch:
            for timeout in (0, -1, 301, float("nan")):
                with self.assertRaises(sources.PreparationError):
                    sources.prepare_package(
                        "jiter", self.dest, lock_path=self.lock_path, timeout=timeout
                    )
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
