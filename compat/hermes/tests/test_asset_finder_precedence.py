"""Execute literal production finder/loader definitions against real ZIP indexes.

Android suffixes are supplied explicitly; loader objects record selection only.
These are host regression tests, not Android/native execution evidence.
"""

import ast
import io
from pathlib import Path
from posixpath import join
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from zipfile import ZipFile, ZipInfo


IMPORTER = (
    Path(__file__).resolve().parents[3]
    / "product/runtime/src/main/python/java/android/importer.py"
)
TARGETS = (
    ("3.13", "aarch64-linux-android"),
    ("3.13", "x86_64-linux-android"),
    ("3.14", "aarch64-linux-android"),
    ("3.14", "x86_64-linux-android"),
)


class SelectedLoader:
    def __init__(self, finder, module_name, zip_info):
        self.finder = finder
        self.module_name = module_name
        self.zip_info = zip_info


class ExtensionSelection(SelectedLoader):
    pass


class BytecodeSelection(SelectedLoader):
    pass


class SourceSelection(SelectedLoader):
    pass


class AssetFinderPrecedenceTests(unittest.TestCase):
    def production(self, python="3.14", triplet="x86_64-linux-android"):
        """Compile the real definitions, avoiding unavailable Java imports."""
        tree = ast.parse(IMPORTER.read_text(encoding="utf-8"), filename=str(IMPORTER))
        finder_class = next(
            item for item in tree.body if isinstance(item, ast.ClassDef)
            and item.name == "AssetFinder"
        )
        method = next(
            item for item in finder_class.body if isinstance(item, ast.FunctionDef)
            and item.name == "find_module"
        )
        loaders = next(
            item for item in tree.body if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "LOADERS"
                    for target in item.targets)
        )
        suffix = f".cpython-{python.replace('.', '')}-{triplet}.so"
        namespace = {
            "join": join,
            "_imp": SimpleNamespace(extension_suffixes=lambda: [suffix, ".abi3.so", ".so"]),
            "ExtensionAssetLoader": ExtensionSelection,
            "SourcelessAssetLoader": BytecodeSelection,
            "SourceAssetLoader": SourceSelection,
        }
        definitions = ast.Module(body=[loaders, method], type_ignores=[])
        exec(compile(definitions, str(IMPORTER), "exec"), namespace)
        return namespace["find_module"], suffix

    def archive(self, label, filenames):
        data = io.BytesIO()
        with ZipFile(data, "w") as output:
            for filename in filenames:
                info = ZipInfo(filename)
                info.comment = label.encode("ascii")
                output.writestr(info, b"Selection fixture: never executed or loaded")
        data.seek(0)
        archive = ZipFile(data)
        self.addCleanup(archive.close)
        return archive

    def finder(self, common=(), abi=(), *, prefix="aiohttp/_websocket"):
        return SimpleNamespace(
            prefix=prefix,
            zip_files=[self.archive("common", common), self.archive("abi", abi)],
            extract_dir=Mock(),
        )

    def test_abi_extension_wins_over_common_source_and_bytecode(self):
        base = "aiohttp/_websocket/reader_c"
        for python, triplet in TARGETS:
            method, native = self.production(python, triplet)
            for source in (".py", ".pyc"):
                with self.subTest(python=python, triplet=triplet, source=source):
                    finder = self.finder([base + source], [base + native])
                    selected = method(finder, "aiohttp._websocket.reader_c")
                    self.assertIsInstance(selected, ExtensionSelection)
                    self.assertEqual(selected.zip_info.filename, base + native)
                    self.assertEqual(selected.zip_info.comment, b"abi")

    def test_package_extension_wins_over_common_init_source(self):
        base = "package/__init__"
        for python, triplet in TARGETS:
            method, native = self.production(python, triplet)
            for source in (".py", ".pyc"):
                with self.subTest(python=python, triplet=triplet, source=source):
                    finder = self.finder([base + source], [base + native], prefix="")
                    selected = method(finder, "package")
                    self.assertIsInstance(selected, ExtensionSelection)
                    self.assertEqual(selected.zip_info.filename, base + native)
                    finder.extract_dir.assert_called_once_with("package")

    def test_nested_package_extension_priority(self):
        method, native = self.production()
        base = "parent/package/__init__"
        finder = self.finder([base + ".pyc"], [base + native], prefix="parent")
        selected = method(finder, "parent.package")
        self.assertIsInstance(selected, ExtensionSelection)
        finder.extract_dir.assert_not_called()

    def test_package_keeps_priority_over_native_module_across_zips(self):
        method, native = self.production()
        finder = self.finder(["package" + native], ["package/__init__.py"], prefix="")
        selected = method(finder, "package")
        self.assertIsInstance(selected, SourceSelection)
        self.assertEqual(selected.zip_info.filename, "package/__init__.py")
        self.assertEqual(selected.zip_info.comment, b"abi")
        finder.extract_dir.assert_called_once_with("package")

    def test_common_package_keeps_priority_over_abi_native_module(self):
        method, native = self.production()
        finder = self.finder(["package/__init__.py"], ["package" + native], prefix="")
        selected = method(finder, "package")
        self.assertIsInstance(selected, SourceSelection)
        self.assertEqual(selected.zip_info.filename, "package/__init__.py")
        self.assertEqual(selected.zip_info.comment, b"common")

    def test_python_specific_then_abi3_then_plain_extension_across_zips(self):
        base = "aiohttp/_websocket/reader_c"
        for python, triplet in TARGETS:
            method, specific = self.production(python, triplet)
            for lower, higher in ((".so", ".abi3.so"), (".so", specific),
                                  (".abi3.so", specific)):
                with self.subTest(python=python, triplet=triplet, lower=lower, higher=higher):
                    finder = self.finder([base + lower], [base + higher])
                    selected = method(finder, "aiohttp._websocket.reader_c")
                    self.assertIsInstance(selected, ExtensionSelection)
                    self.assertEqual(selected.zip_info.filename, base + higher)
                    self.assertEqual(selected.zip_info.comment, b"abi")

    def test_plain_and_abi3_extensions_also_beat_common_python(self):
        method, _ = self.production()
        base = "aiohttp/_websocket/reader_c"
        for extension in (".so", ".abi3.so"):
            for python_suffix in (".py", ".pyc"):
                with self.subTest(extension=extension, python_suffix=python_suffix):
                    finder = self.finder([base + python_suffix], [base + extension])
                    selected = method(finder, "aiohttp._websocket.reader_c")
                    self.assertIsInstance(selected, ExtensionSelection)
                    self.assertEqual(selected.zip_info.filename, base + extension)

    def test_same_suffix_preserves_common_zip_priority(self):
        method, specific = self.production()
        base = "aiohttp/_websocket/reader_c"
        for suffix in (specific, ".abi3.so", ".so", ".pyc", ".py"):
            with self.subTest(suffix=suffix):
                finder = self.finder([base + suffix], [base + suffix])
                selected = method(finder, "aiohttp._websocket.reader_c")
                self.assertEqual(selected.zip_info.comment, b"common")
                self.assertEqual(selected.zip_info.filename, base + suffix)

    def test_same_suffix_preserves_first_of_three_archives(self):
        method, specific = self.production()
        base = "module" + specific
        finder = SimpleNamespace(
            prefix="", extract_dir=Mock(),
            zip_files=[self.archive(name, [base]) for name in ("unsuffixed", "common", "abi")],
        )
        selected = method(finder, "module")
        self.assertEqual(selected.zip_info.comment, b"unsuffixed")

    def test_bytecode_priority_applies_across_archives(self):
        method, _ = self.production()
        base = "aiohttp/_websocket/reader_c"
        finder = self.finder([base + ".py"], [base + ".pyc"])
        selected = method(finder, "aiohttp._websocket.reader_c")
        self.assertIsInstance(selected, BytecodeSelection)
        self.assertEqual(selected.zip_info.comment, b"abi")

    def test_same_archive_still_prefers_native(self):
        method, native = self.production()
        base = "aiohttp/_websocket/reader_c"
        finder = self.finder([base + ".py", base + ".pyc", base + native])
        selected = method(finder, "aiohttp._websocket.reader_c")
        self.assertIsInstance(selected, ExtensionSelection)
        self.assertEqual(selected.zip_info.comment, b"common")

    def test_wrong_cpython_suffix_is_not_selected(self):
        method, _ = self.production("3.14", "x86_64-linux-android")
        base = "aiohttp/_websocket/reader_c"
        for wrong in (".cpython-313-x86_64-linux-android.so",
                      ".cpython-314-aarch64-linux-android.so"):
            with self.subTest(wrong=wrong):
                finder = self.finder([base + ".py"], [base + wrong])
                selected = method(finder, "aiohttp._websocket.reader_c")
                self.assertIsInstance(selected, SourceSelection)

    def test_no_matching_module_returns_none(self):
        method, _ = self.production()
        finder = self.finder(["aiohttp/_websocket/other.py"], ["different/module.so"])
        self.assertIsNone(method(finder, "aiohttp._websocket.reader_c"))


if __name__ == "__main__":
    unittest.main()
