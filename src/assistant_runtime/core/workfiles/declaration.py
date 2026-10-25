from assistant_runtime.api.files import FileRequest
from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec


def build(host):
    from . import WorkFiles, WorkFilesMaintenance

    host.workfiles = WorkFiles(host.config.state_dir, host.store.db, host.config.work_files)
    return WorkFilesMaintenance(host.workfiles)


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    internal_factory=build,
    provides=(FileRequest,),
    configuration=configuration_spec(__file__),
)
