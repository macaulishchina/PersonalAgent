/** Host-bound controlled resources: no host paths, grants or caller identities. */
import { Type } from "typebox";

export function registerControlled(pi: any) {
  const token = process.env.ASSISTANT_CONTROL_TOKEN;
  if (!token) return;
  const area = Type.Union(["source", "work", "inputs", "artifacts"].map(v => Type.Literal(v)));
  const definitions = [
    {name: "pa_read", action: "read", description: "Read UTF-8 text in an authorized area. Offset and length are bytes; use next_offset for the next chunk.",
      parameters: Type.Object({area, path: Type.String(), offset: Type.Optional(Type.Integer({minimum:0})), length: Type.Optional(Type.Integer({minimum:1,maximum:16384}))})},
    {name: "pa_write", action: "write", description: "Write UTF-8 text in work or artifacts. Provide expected_sha256 from the last read; null means create a new file. Never repeat an unknown write.",
      parameters: Type.Object({area, path: Type.String(), text: Type.String(), expected_sha256: Type.Union([Type.String(),Type.Null()])})},
    {name: "pa_check", action: "check", description: "Run a trusted, preconfigured fixed check by name. Arbitrary shell commands are not accepted.",
      parameters: Type.Object({name: Type.String()})},
  ];
  for (const tool of definitions) pi.registerTool({
    ...tool, label: tool.name,
    execute: async (id: string, parameters: any, signal: AbortSignal) => {
      const response = await fetch(process.env.ASSISTANT_CONTROL_URL + "/resources", {
        method: "POST", signal,
        headers: {"Content-Type":"application/json", Authorization:"Bearer " + process.env.PA_EXECUTION_TOKEN,
          "x-assistant-session":process.env.ASSISTANT_CONTROL_SESSION || "", "x-assistant-control":token},
        body: JSON.stringify({operation_id:id,action:tool.action,arguments: tool.action === "read" ? {...parameters,length:parameters.length ?? 16384} : parameters}),
      });
      const result = await response.json();
      return {content:[{type:"text",text:JSON.stringify(result)}],isError:!response.ok || result.state !== "completed",details:{operation_id:result.operation_id}};
    },
  });
}
