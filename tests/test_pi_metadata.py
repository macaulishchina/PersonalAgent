"""Native durable Pi metadata writes; no provider request."""

import json
from pathlib import Path
import tempfile
import unittest

from assistant_runtime.core.pi.rpc import PiClient
from platform_support import private_mode


class MetadataTest(unittest.TestCase):
    def test_metadata_uses_private_atomic_write_without_temporary_residue(self):
        with tempfile.TemporaryDirectory() as folder:
            client = PiClient.__new__(PiClient)
            client.runtime_dir = Path(folder)
            client._save_meta("abcd", {"value": "before"})
            client._save_meta("abcd", {"value": "after"})
            path = Path(folder) / "abcd/metadata.json"
            self.assertEqual(json.loads(path.read_text()), {"value": "after"})
            self.assertEqual(private_mode(path), 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])
