"""Cloud identity and mounted paths are independent evidence, for every provider."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.openlist import OpenList
from assistant_runtime.plugins.media.verification import readback


class VerificationTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, provider="quark"):
        target = {"account": provider, "folder_id": "series", "openlist_path": "/mounted/电视剧"}
        file = {
            "file_id": "saved",
            "parent_file_id": "series",
            "name": "E01.mp4",
            "type": "file",
            "size": 100,
        }
        plugin = SimpleNamespace(
            target=lambda name: target, provider=SimpleNamespace(call=AsyncMock(return_value=file))
        )
        item = {"data": {"intent": {"target": "shows", "destination": dict(target)}, "file": dict(file)}}
        return plugin, item, file

    async def test_every_provider_requires_destination_readback(self):
        for provider in ("aliyun", "quark", "future_provider"):
            plugin, item, file = self.fixture(provider)
            actual, evidence = await readback(plugin, item, "saved", "provider_file_id")
            self.assertEqual(actual, file)
            self.assertEqual(evidence["state"], "verified")
            self.assertEqual(evidence["account"], provider)
            self.assertIn("parent_file_id", evidence["checks"])
            plugin.provider.call.assert_awaited_once_with(
                "file", {**item["data"]["intent"]["destination"], "file_id": "saved"}
            )

    async def test_ack_or_stale_listing_cannot_hide_wrong_saved_file(self):
        for field, wrong in (
            ("file_id", "different"),
            ("parent_file_id", "wrong-folder"),
            ("name", "E02.mp4"),
            ("size", 99),
            ("type", "folder"),
        ):
            plugin, item, file = self.fixture()
            file[field] = wrong
            with self.assertRaisesRegex(MediaError, "cloud_file_verification_mismatch"):
                await readback(plugin, item, "saved", "provider_file_id")

    async def test_source_checksum_must_survive_final_readback(self):
        plugin, item, file = self.fixture()
        item["data"]["file"].update(content_hash="a" * 40, content_hash_name="sha1")
        file.update(content_hash="b" * 40, content_hash_name="sha1")
        with self.assertRaisesRegex(MediaError, "cloud_file_checksum_mismatch"):
            await readback(plugin, item, "saved", "content_hash")

    async def test_mapping_change_does_not_change_cloud_identity(self):
        plugin, item, _ = self.fixture()
        plugin.target("shows")["openlist_path"] = "/another-mount/series"
        _, evidence = await readback(plugin, item, "saved", "provider_file_id")
        self.assertEqual(evidence["state"], "verified")
        plugin.target("shows")["folder_id"] = "another-folder"
        with self.assertRaisesRegex(MediaError, "target_configuration_changed"):
            await readback(plugin, item, "saved", "provider_file_id")

    async def test_mount_roots_are_explicit_and_unverified_paths_are_not_published(self):
        for root in ("/夸克网盘/电视剧", "/阿里/电视剧", "/custom-root"):
            adapter = OpenList(None, "http://localhost:5244", Path("unused"))
            metadata = {"name": "E01.mp4", "size": 100, "is_dir": False}
            adapter.post = AsyncMock(side_effect=[{}, metadata])
            result = await adapter.verify({"openlist_path": root}, {"name": "E01.mp4", "size": 100})
            self.assertEqual(result["path"], root + "/E01.mp4")
            self.assertTrue(result["path_verified"])
            self.assertEqual(result["path_scope"], "openlist_mount")
            adapter.post.assert_awaited_with("/api/fs/get", {"path": root + "/E01.mp4"}, scope=True)
            adapter.post = AsyncMock(side_effect=[{}, {**metadata, "size": 99}])
            result = await adapter.verify({"openlist_path": root}, {"name": "E01.mp4", "size": 100})
            self.assertFalse(result["path_verified"])
            self.assertNotIn("path", result)
            self.assertEqual(result["configured_path"], root + "/E01.mp4")

    async def test_same_size_wrong_hash_is_not_a_verified_mapped_file(self):
        adapter = OpenList(None, "http://localhost:5244", Path("unused"))
        file = {"name": "E01.mp4", "size": 100, "content_hash": "a" * 40, "content_hash_name": "sha1"}
        adapter.post = AsyncMock(
            side_effect=[
                {},
                {"name": "E01.mp4", "size": 100, "is_dir": False, "hash_info": {"sha1": "b" * 40}},
            ]
        )
        result = await adapter.verify({"openlist_path": "/shows"}, file)
        self.assertEqual(result["state"], "mismatch")
        self.assertNotIn("path", result)


if __name__ == "__main__":
    unittest.main()
