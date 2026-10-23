"""Application assets are explicit and isolated from Coding AI project configuration."""

from pathlib import Path
import tempfile
import unittest

from assistant_runtime.app.bootstrap import assistant_skill_paths
from assistant_runtime.app.config import Config


class AssistantSkillsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def skill(self, relative):
        path = self.root / relative
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text("---\nname: private-device\ndescription: Private device steps.\n---\n")
        return path

    def test_enabled_owners_only_and_private_skills_require_explicit_selection(self):
        self.skill(".agents/skills/hsdd-epic")
        private = self.skill("config/skills/private-device")
        config = Config(root=self.root, plugins=["tasks", "events"])
        selected = assistant_skill_paths(config)
        self.assertEqual([p.name for p in selected], ["event-driven-tasks", "event-management"])
        self.assertTrue(all((p / "SKILL.md").is_file() for p in selected))
        self.assertNotIn(private, selected)
        config.instance_skills = ["private-device"]
        self.assertEqual(assistant_skill_paths(config), (*selected, private))
        config.plugins = []
        self.assertEqual(assistant_skill_paths(config), (private,))

    def test_invalid_selection_missing_skills_and_shadowing_fail_explicitly(self):
        for names in ("device", [1], ["../hsdd-epic"], ["/tmp/skill"], ["a", "a"], ["a"] * 33):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "instance_skills"):
                Config(root=self.root, instance_skills=names)
        config = Config(root=self.root, plugins=["tasks"], instance_skills=["missing"])
        with self.assertRaisesRegex(ValueError, "must exist"):
            assistant_skill_paths(config)
        self.skill("config/skills/event-driven-tasks")
        config.instance_skills = ["event-driven-tasks"]
        with self.assertRaisesRegex(ValueError, "shadow"):
            assistant_skill_paths(config)

    def test_symlinks_cannot_load_project_skills_or_external_entry_files(self):
        coding = self.skill(".agents/skills/hsdd-epic")
        base = self.root / "config/skills"
        base.mkdir(parents=True)
        (base / "coding").symlink_to(coding, target_is_directory=True)
        config = Config(root=self.root, plugins=[], instance_skills=["coding"])
        with self.assertRaisesRegex(ValueError, "inside config/skills"):
            assistant_skill_paths(config)
        folder = base / "private-device"
        folder.mkdir()
        (folder / "SKILL.md").symlink_to(coding / "SKILL.md")
        config.instance_skills = ["private-device"]
        with self.assertRaisesRegex(ValueError, "inside config/skills"):
            assistant_skill_paths(config)
