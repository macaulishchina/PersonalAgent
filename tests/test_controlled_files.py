"""Native workspace path/compare-and-write checks; synthetic directories only."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import uuid

from assistant_runtime.core.execution.controlled_files import ControlledFiles


@unittest.skipUnless(os.name == "nt", "Windows controlled-file backend")
class FilesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.files = ControlledFiles(SimpleNamespace(files=SimpleNamespace(root=Path(self.temporary.name))))
        self.workspace = str(uuid.uuid4())
        self.root = self.files.ensure(self.workspace)

    def test_exclusive_hash_guard_and_traversal_rejection(self):
        work = self.files.area(self.workspace, "work")
        saved = self.files.write(work, "nested/code.py", "original", None)
        self.assertEqual(self.files.read(work, "nested/code.py")["text"], "original")
        with self.assertRaises(ValueError):
            self.files.write(work, "nested/code.py", "replacement", "0" * 64)
        self.assertEqual((work / "nested/code.py").read_text(), "original")
        replaced = self.files.write(work, "nested/code.py", "new", saved["sha256"])
        self.assertEqual(replaced["sha256"], hashlib.sha256(b"new").hexdigest())
        for path in ("../secret", "nested/../../secret", "C:/secret", "nested/a:stream", "NUL", "/absolute"):
            with self.assertRaises(ValueError):
                self.files.read(work, path)
        with self.assertRaises(PermissionError):
            self.files.write(work, "rejected", "text", None, authorized=lambda: False)
        self.assertFalse((work / "rejected").exists())

    def test_hard_links_open_writers_and_independent_round_areas(self):
        work = self.files.area(self.workspace, "work")
        saved = self.files.write(work, "report", "original", None)
        os.link(work / "report", work / "alias")
        with self.assertRaises(ValueError):
            self.files.read(work, "report")
        with self.assertRaises(ValueError):
            self.files.write(work, "report", "changed", saved["sha256"])
        (work / "alias").unlink()
        with (work / "report").open("r+b"):
            with self.assertRaises(PermissionError):
                self.files.read(work, "report")
        first = self.files.execution_area(self.workspace, str(uuid.uuid4()))
        second = self.files.execution_area(self.workspace, str(uuid.uuid4()))
        self.assertNotEqual(first, second)
        self.assertTrue((first / "artifacts").is_dir())
        self.assertTrue((second / "temporary").is_dir())
