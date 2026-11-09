"""An independently installed configuration-page acceptance plugin. No host imports."""

from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec, ConfigurationUpdate, ConfigurationError


class Demo:
    def __init__(self, context):
        self.context = context

    async def start(self):
        pass

    async def close(self):
        pass

    async def prepare_configuration(self, settings):
        return ConfigurationUpdate()

    async def configuration_action(self, action, arguments):
        if action == "status":
            return {"label": self.context.config["label"], "registered": bool(self.context.get("device"))}
        if action == "register":
            self.context.set("device", arguments["name"])
            self.context.publish("configuration_demo.registered", registered=True)
            return {"registered": True}
        raise ConfigurationError([], "unknown_action", "未知操作")


def validate(settings):
    if settings["mode"] == "remote" and not settings["endpoint"].startswith(("http://", "https://")):
        raise ConfigurationError(
            ["plugin_settings", "configuration_demo", "endpoint"], "endpoint", "远端地址需使用 HTTP 或 HTTPS"
        )
    return settings


def definition():
    return PluginDefinition(
        manifest=plugin_manifest(__file__),
        create=Demo,
        validate=validate,
        configuration=configuration_spec(__file__),
    )
