// Responses may compile tool parameters even when the SDK omits "strict".
// Project validation-only conditions on the wire; the MCP schema still owns execution validation.
const LOCAL_CONSTRAINTS = new Set([
  "if", "then", "else", "not", "dependentRequired", "dependentSchemas",
]);
const SCHEMA_MAPS = new Set(["properties", "patternProperties", "$defs", "definitions"]);
const SCHEMA_LISTS = new Set(["allOf", "anyOf", "oneOf", "prefixItems"]);
const SCHEMA_CHILDREN = new Set([
  "items", "additionalItems", "additionalProperties", "contains", "propertyNames",
  "unevaluatedItems", "unevaluatedProperties",
]);

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function projectSchema(schema) {
  if (!isObject(schema)) return schema;
  let projected = schema;
  function update(key, value) {
    if (projected === schema) projected = { ...schema };
    if (value === undefined) delete projected[key];
    else projected[key] = value;
  }
  for (const [key, value] of Object.entries(schema)) {
    if (LOCAL_CONSTRAINTS.has(key)) {
      update(key, undefined);
    } else if (SCHEMA_MAPS.has(key) && isObject(value)) {
      let fields = value;
      for (const [name, field] of Object.entries(value)) {
        const next = projectSchema(field);
        if (next === field) continue;
        if (fields === value) fields = { ...value };
        fields[name] = next;
      }
      if (fields !== value) update(key, fields);
    } else if (SCHEMA_LISTS.has(key) && Array.isArray(value)) {
      const branches = value.map(projectSchema);
      if (branches.every((branch, index) => branch === value[index])) {
        // An intersection that defines argument structure still needs best-effort mode.
        if (key === "allOf") update(key, value);
        continue;
      }
      // Remove neutral branches left by condition-only conjuncts. Keep structural branches.
      const remaining = key === "allOf"
        ? branches.filter((branch, index) => branch === value[index] || Object.keys(branch).length)
        : branches;
      update(key, remaining.length ? remaining : undefined);
    } else if (SCHEMA_CHILDREN.has(key)) {
      const next = projectSchema(value);
      if (next !== value) update(key, next);
    }
  }
  return projected;
}

function projectTools(tools) {
  if (!Array.isArray(tools)) return tools;
  return tools.map((tool) => {
    if (tool?.type === "namespace")
      return { ...tool, tools: projectTools(tool.tools) };
    if (tool?.type !== "function") return tool;
    const parameters = projectSchema(tool.parameters);
    if (parameters === tool.parameters) return tool;
    // Preserve optional arguments rather than relying on upstream strict normalization.
    return { ...tool, parameters, strict: false };
  });
}

export function projectResponsesTools(payload) {
  if (Array.isArray(payload.tools)) payload.tools = projectTools(payload.tools);
  if (!Array.isArray(payload.input)) return;
  for (const item of payload.input) {
    if (
      (item?.type === "additional_tools" || item?.type === "tool_search_output") &&
      Array.isArray(item.tools)
    )
      item.tools = projectTools(item.tools);
  }
}
