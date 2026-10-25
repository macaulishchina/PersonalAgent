"""Shared operator configuration service; never constructs a business plugin."""

from .files import ConfigurationFiles
from .resources import Resources
from .validation import validate_candidate
from .fields import present


class ConfigurationService:
    def __init__(self, settings):
        self.settings = settings
        self.resources = Resources(settings.root)
        self.files = ConfigurationFiles(
            settings.entry,
            settings.root / "state/configuration",
            lambda contents: validate_candidate(
                settings.entry.name, contents, root=settings.root, entry_directory=settings.entry.parent
            ),
            resources=self.resources,
        )

    def read(self):
        with self.files.locked():
            snapshot = self.files.snapshot()
            result = present(snapshot, self.files.entry.name, root=self.settings.root)
            result["resources"] = self.resources.public(snapshot.contents, snapshot.resources)
            return result

    def check(self):
        with self.files.locked():
            snapshot = self.files.snapshot()
            self.files.validate_all(snapshot.contents, snapshot.resources)
            return {"valid": True, "version": snapshot.version}
