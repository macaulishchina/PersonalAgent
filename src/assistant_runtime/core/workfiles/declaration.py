from assistant_runtime.api.files import FileRequest
from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    from . import WorkFiles, WorkFilesMaintenance

    host.workfiles = WorkFiles(host.config.state_dir, host.store.db, host.config.work_files)
    return WorkFilesMaintenance(host.workfiles)


DECLARATION = PluginDefinition(Manifest("workfiles"), internal_factory=build, provides=(FileRequest,))
