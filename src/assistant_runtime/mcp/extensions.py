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
    """Refresh enabled tools at discovery; never import extension packages in an agent helper."""
    from fastmcp.server.providers import Provider

    class RemoteTools(Provider):
        async def _list_tools(self):
            metadata = await client.request({"command": "plugin-tools"})
            tools = []
            for spec in metadata["tools"]:

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
            return tools

        async def _get_tool(self, name, version=None):
            return next((tool for tool in await self._list_tools() if tool.name == name), None)

    server = FastMCP("extensions")
    server.add_provider(RemoteTools())
    return server
