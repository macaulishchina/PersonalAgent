"""MCP projection of the same public tool declarations used by direct calls."""

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool


def create_server(registry, identity):
    server = FastMCP("extensions")
    for name, (_, tool) in registry.tools.items():

        def bind(name):
            async def invoke(**arguments):
                return await registry.invoke(name, arguments, identity=identity())

            return invoke

        server.add_tool(
            FunctionTool(
                name=name,
                description=tool.description,
                parameters=dict(tool.input_schema),
                output_schema=dict(tool.output_schema),
                annotations=tool.annotations,
                fn=bind(name),
            )
        )
    return server


def create_remote_server(client):
    """Project the live registry without importing or instantiating extension owners."""
    from fastmcp.server.providers import Provider

    class RemoteTools(Provider):
        catalog = None
        tools = ()

        async def get_tasks(self):
            # These tools return ordinary MCP results. Lifespan setup has no caller identity.
            return []

        async def _list_tools(self):
            catalog = (await client.request({"command": "plugin-tools"}))["tools"]
            if self.catalog == catalog:
                return self.tools
            tools = []
            for spec in catalog:

                def bind(name):
                    async def invoke(**arguments):
                        return await client.request(
                            {"command": "plugin-invoke", "name": name, "arguments": arguments}
                        )

                    return invoke

                tools.append(
                    FunctionTool(
                        name=spec["name"],
                        description=spec["description"],
                        parameters=spec["input_schema"],
                        output_schema=spec["output_schema"],
                        annotations=spec["annotations"],
                        fn=bind(spec["name"]),
                    )
                )
            self.catalog = catalog
            self.tools = tuple(tools)
            return self.tools

        async def _get_tool(self, name, version=None):
            return next((tool for tool in await self._list_tools() if tool.name == name), None)

    server = FastMCP("extensions")
    server.add_provider(RemoteTools())
    return server
