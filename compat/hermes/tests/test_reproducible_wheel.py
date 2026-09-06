import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from compat.hermes import reproducible_wheel


class ReproducibleWheelTests(unittest.TestCase):
    def test_only_exact_source_root_changes_and_references_stay_consistent(self):
        source = Path("source").resolve()
        reference = "path+" + source.as_uri() + "/crate#1.0"
        data = {"component": {"bom-ref": reference}, "dependencies": [{"ref": reference}],
                "untouched": "path+" + source.as_uri() + "-other/crate", "number": 1}
        rewritten = reproducible_wheel.relocate_source(data, source)
        self.assertEqual(rewritten["component"]["bom-ref"], "path+file:///hermes-source/crate#1.0")
        self.assertEqual(rewritten["dependencies"][0]["ref"], rewritten["component"]["bom-ref"])
        self.assertEqual(rewritten["untouched"], data["untouched"])
        self.assertEqual(data["component"]["bom-ref"], reference)

    def test_different_roots_produce_identical_wheels_without_changing_code_or_license(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheels = []
            for name in ("first", "other"):
                source = root / name
                path = root / (name + ".whl")
                bom = {"bomFormat": "CycloneDX", "components": [{"bom-ref": "path+" + source.as_uri() + "#1"}]}
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("package/native.so", b"unchanged native code")
                    archive.writestr("package-1.dist-info/licenses/LICENSE", b"original license")
                    archive.writestr("package-1.dist-info/sboms/package.cyclonedx.json", json.dumps(bom))
                    archive.writestr("package-1.dist-info/RECORD", "original audited record")
                reproducible_wheel.normalize(path, source)
                wheels.append(path.read_bytes())
                with zipfile.ZipFile(path) as archive:
                    self.assertEqual(archive.read("package/native.so"), b"unchanged native code")
                    self.assertEqual(archive.read("package-1.dist-info/licenses/LICENSE"), b"original license")
                    rows = list(csv.reader(io.StringIO(archive.read("package-1.dist-info/RECORD").decode())))
                    self.assertEqual({row[0] for row in rows}, set(archive.namelist()))
                    for member, digest, size in rows:
                        if digest:
                            import base64
                            self.assertEqual(digest, "sha256=" + base64.urlsafe_b64encode(
                                hashlib.sha256(archive.read(member)).digest()).rstrip(b"=").decode())
                            self.assertEqual(int(size), len(archive.read(member)))
            self.assertEqual(*wheels)
