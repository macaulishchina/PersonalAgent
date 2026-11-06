// Pi's provider library and credential store only. This process has no tools or agent loop.
import { createInterface } from "node:readline";
import { once } from "node:events";
import { mkdir, readFile, writeFile, chmod } from "node:fs/promises";
import { dirname, join } from "node:path";
import { findPackageJSON } from "node:module";
import { pathToFileURL } from "node:url";
import { randomUUID, createHash } from "node:crypto";
import {
  observeFetch,
  observeError,
  failure,
  reliableRequest,
  attemptContext,
} from "./reliability.mjs";

// Never forward configured credentials to a redirected host.
const nativeFetch = globalThis.fetch;
globalThis.fetch = observeFetch(nativeFetch, () => argsKey);
const installation = process.argv[2];
const load = (path) => import(pathToFileURL(join(installation, path)).href);
// npm ci may hoist dependencies alongside the agent instead of nesting them.
const aiRoot = dirname(
  findPackageJSON(
    "@earendil-works/pi-ai",
    pathToFileURL(join(installation, "package.json")),
  ),
);
const loadAI = (path) => import(pathToFileURL(join(aiRoot, "dist", path)).href);
const ai = await loadAI("index.js");
const { retryProviderRequest } = await loadAI("utils/provider-retry.js");
const { builtinProviders } = await loadAI("providers/all.js");
const { AuthStorage } = await load("dist/core/auth-storage.js");
const providers = builtinProviders();
let runtime, spec, folder, storage, argsKey;
const active = new Map(),
  prompts = new Map(),
  permits = new Map();
let writing = Promise.resolve();
function emit(value) {
  writing = writing.then(async () => {
    if (!process.stdout.write(JSON.stringify(value) + "\n"))
      await once(process.stdout, "drain");
  });
  return writing;
}

function provider(id) {
  const found = providers.find((p) => p.id === id);
  if (!found) throw new Error("native_provider_unavailable");
  return found;
}

async function init(args) {
  spec = args.spec;
  argsKey = args.key;
  folder = args.folder;
  await mkdir(folder, { recursive: true, mode: 0o700 });
  await chmod(folder, 0o700);
  storage = AuthStorage.create(join(folder, "auth.json"));
  if (spec.protocol && spec.protocol !== "pi") return initConnection(args);
  const selected = provider(spec.provider);
  const method = spec.auth === "api_key" ? "apiKey" : "oauth";
  if (!selected.auth[method]) throw new Error("native_auth_method_unavailable");
  // Never fall back from OAuth to an ambient API key, or to another global account.
  const credentials = {
    read: async (id, options) => {
      if (spec.key_env) return { type: "api_key", key: args.key };
      const result = await storage.read(id, options);
      if (result && result.type !== spec.auth)
        throw new Error("native_account_auth_mismatch");
      return result;
    },
    list: (...a) => storage.list(...a),
    modify: (...a) => storage.modify(...a),
    delete: (...a) => storage.delete(...a),
  };
  runtime = ai.createModels({
    credentials,
    authContext: { env: async () => undefined, fileExists: async () => false },
  });
  runtime.setProvider({
    ...selected,
    auth: { [method]: selected.auth[method] },
  });
  return { initialized: true };
}

async function initConnection(args) {
  const protocols = {
    openai_responses: ["openai-responses", "openAIResponsesApi"],
    openai_chat: ["openai-completions", "openAICompletionsApi"],
    anthropic: ["anthropic-messages", "anthropicMessagesApi"],
    gemini: ["google-generative-ai", "googleGenerativeAIApi"],
  };
  const [api, factory] = protocols[spec.protocol] || [];
  if (!api) throw new Error("unsupported_connection_protocol");
  const implementation = await loadAI(`api/${api}.lazy.js`);
  const { envApiKeyAuth } = await loadAI("auth/helpers.js");
  runtime = ai.createModels({
    credentials: { read: async () => ({ type: "api_key", key: args.key }) },
    authContext: { env: async () => undefined, fileExists: async () => false },
  });
  // Reuse only Pi's wire-mode declaration for this exact model. Provider auth,
  // endpoints, capabilities and managed conversation policies stay route-owned.
  const anthropicModels =
    spec.protocol === "anthropic"
      ? providers.find((p) => p.id === "anthropic")?.getModels() || []
      : [];
  const models = Object.entries(spec.entries).map(([alias, entry]) => ({
    id: alias,
    name: entry.model,
    api,
    provider: spec.provider,
    baseUrl: spec.base_url,
    reasoning:
      entry.reasoning_efforts.some((level) => level !== "none") ||
      (spec.protocol === "anthropic" &&
        entry.reasoning_efforts.includes("none")),
    thinkingLevelMap: Object.fromEntries(
      ["none", "minimal", "low", "medium", "high", "xhigh", "max"].map(
        (level) => [
          level === "none" ? "off" : level,
          entry.reasoning_efforts.includes(level) ? level : null,
        ],
      ),
    ),
    ...(anthropicModels.find((m) => m.id === entry.model)?.compat
      ?.forceAdaptiveThinking === true
      ? { compat: { forceAdaptiveThinking: true } }
      : {}),
    input: entry.images ? ["text", "image"] : ["text"],
    contextWindow: entry.context_window || 32768,
    maxTokens: entry.output_limit || 131072,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  }));
  runtime.setProvider(
    ai.createProvider({
      id: spec.provider,
      baseUrl: spec.base_url,
      auth: { apiKey: envApiKeyAuth("Configured API key", []) },
      models,
      api: implementation[factory](),
    }),
  );
  return { initialized: true };
}

function modelMetadata(m) {
  return {
    id: m.id,
    api: m.api,
    images: m.input.includes("image"),
    reasoning: !!m.reasoning,
    reasoning_efforts: ai
      .getSupportedThinkingLevels(m)
      .map((e) => (e === "off" ? "none" : e)),
    context_window: m.contextWindow,
    output_limit: m.maxTokens,
    max_output_tokens: m.maxTokens,
  };
}

function modelFor(args) {
  const configured = runtime.getModel(
    spec.provider,
    spec.entries ? args.alias : args.model,
  );
  const model =
    configured && spec.entries ? { ...configured, id: args.model } : configured;
  if (!model) throw new Error("native_model_not_in_catalog");
  const effort =
    args.effort === "auto"
      ? undefined
      : args.effort === "none"
        ? "off"
        : args.effort;
  if (
    effort !== undefined &&
    !ai.getSupportedThinkingLevels(model).includes(effort)
  )
    throw new Error("native_reasoning_effort_unsupported");
  if (args.images && !model.input.includes("image"))
    throw new Error("native_image_input_unsupported");
  if (
    args.schema &&
    ![
      "openai-responses",
      "openai-completions",
      "anthropic-messages",
      "google-generative-ai",
    ].includes(model.api)
  ) {
    throw new Error("native_structured_output_unsupported");
  }
  return { model, effort };
}

async function runAttempt(id, args, signal, evidence) {
  const started = performance.now();
  const timings = {};
  const elapsed = () => Math.round(performance.now() - started);
  const { model: selectedModel, effort } = modelFor(args);
  const diagnostic = (name, fields = {}) => emit({ id, event: {type:"diagnostic", name,
    model:args.alias, session_id:args.session_id, effort:args.effort, ...fields} });
  await diagnostic("model.request.started");
  // Pi's legacy Anthropic adapter adds thinking tokens to maxTokens. Constrain
  // its shared ceiling first so it fits the budget instead of enlarging our cap.
  const model =
    spec.protocol === "anthropic" && spec.entries && args.max_tokens
      ? {
          ...selectedModel,
          maxTokens: Math.min(selectedModel.maxTokens, args.max_tokens),
        }
      : selectedModel;
  const context = structuredClone(args.context);
  // Prepare a detached input view; Pi owns cross-provider conversion.
  for (const message of context.messages) {
    if (
      spec.protocol === "openai_responses" &&
      message.role === "assistant" &&
      Array.isArray(message.content)
    ) {
      // Some Responses bridges emit an empty text item after tool calls. Replaying
      // it creates a separate empty assistant message before the tool outputs.
      // Omit only zero-length text in this detached input view; preserve actual
      // text, call IDs, results and persisted/event ordering.
      message.content = message.content.filter(
        (block) => block.type !== "text" || block.text !== "",
      );
    }
    if (message.role === "assistant" && message.model === args.alias) {
      // Restore native identity only for this alias's own history.
      Object.assign(message, {
        provider: model.provider,
        model: model.id,
        api: model.api,
      });
    }
  }
  const buffered = [];
  const options = {
    signal,
    maxRetries: 0,
    timeoutMs: args.timeout_ms,
    transport: "sse",
    maxTokens: args.max_tokens || undefined,
    // Anthropic's simple adapter uses absent reasoning for disabled thinking;
    // passing the literal "off" is truthy and would enable adaptive thinking.
    reasoning:
      model.api === "anthropic-messages" && effort === "off"
        ? undefined
        : effort,
    // Reuse already processed input through Pi's normal provider cache/session
    // affinity. This does not enable cache warming or automatic model requests.
    cacheRetention: "short",
    sessionId: args.session_id || undefined,
    transformHeaders: async (headers) => {
      if (!spec.protocol || spec.protocol === "pi") return headers;
      const result = { ...headers };
      if (spec.auth_scheme === "bearer") {
        for (const name of Object.keys(result))
          if (
            ["authorization", "x-api-key", "x-goog-api-key"].includes(
              name.toLowerCase(),
            )
          )
            delete result[name];
        result.Authorization = `Bearer ${argsKey}`;
      }
      if (spec.session_header) {
        if (!args.session_id) throw new Error("connection_session_required");
        result[spec.session_header] = createHash("sha256")
          .update(args.session_id)
          .digest("hex");
      }
      return result;
    },
    onPayload: async (payload) => {
      if (signal.aborted) throw new Error("native_cancelled");
      if (spec.entries && args.effort === "auto") {
        delete payload.reasoning;
        delete payload.reasoning_effort;
        delete payload.thinking;
        if (payload.config) delete payload.config.thinkingConfig;
        if (payload.generationConfig)
          delete payload.generationConfig.thinkingConfig;
      }
      if (args.schema) {
        const format = {
          type: "json_schema",
          name: "result",
          schema: args.schema,
          strict: true,
        };
        if (model.api === "openai-responses")
          payload.text = { ...payload.text, format };
        else if (model.api === "openai-completions")
          payload.response_format = {
            type: "json_schema",
            json_schema: { name: "result", schema: args.schema, strict: true },
          };
        else if (model.api === "anthropic-messages")
          payload.output_config = {
            ...payload.output_config,
            format: { type: "json_schema", schema: args.schema },
          };
        else
          payload.config = {
            ...payload.config,
            responseMimeType: "application/json",
            responseJsonSchema: args.schema,
          };
      }
      if (
        model.api === "anthropic-messages" &&
        payload.thinking?.type === "enabled" &&
        (payload.thinking.budget_tokens < 1024 ||
          payload.thinking.budget_tokens >= payload.max_tokens)
      )
        throw new Error("anthropic_thinking_budget_insufficient");
      if (!args.max_tokens) {
        // SDK simple adapters fill defaults; optional limits stay supplier-owned.
        if (["openai-responses", "openai-completions"].includes(model.api)) {
          delete payload.max_output_tokens;
          delete payload.max_tokens;
          delete payload.max_completion_tokens;
        } else if (model.api === "google-generative-ai") {
          if (payload.config) delete payload.config.maxOutputTokens;
          if (payload.generationConfig)
            delete payload.generationConfig.maxOutputTokens;
        }
        // Anthropic requires max_tokens; keep Pi's model/context-derived value.
      }
      if (args.max_tokens && spec.output_limit !== "model") {
        const cap =
          payload.max_output_tokens ??
          payload.max_tokens ??
          payload.max_completion_tokens ??
          payload.config?.maxOutputTokens ??
          payload.generationConfig?.maxOutputTokens ??
          payload.inferenceConfig?.maxTokens;
        if (!Number.isInteger(cap) || cap < 1 || cap > args.max_tokens)
          throw new Error("native_request_output_limit_unsupported");
      }
      timings.request_bytes = Buffer.byteLength(JSON.stringify(payload));
      timings.prepared_ms = elapsed();
      // Authentication may have used HTTP before model payload preparation.
      evidence.submission = "not_submitted";
      delete evidence.status;
      delete evidence.request_id;
      delete evidence.headers;
      delete evidence.provider_code;
      delete evidence.provider_message;
      // Payload preparation is not evidence of an HTTP submission.
      await emit({ id, event: { type: "dispatch" } });
      return payload;
    },
    onProviderStreamEvent: (data) => {
      // Keep metadata only; never retain or log provider payloads/deltas.
      if (typeof data?.type === "string" && /^[a-z_.]{1,96}$/.test(data.type))
        evidence.last_event = data.type;
      if (["response.completed","response.failed","response.incomplete","error"].includes(data?.type))
        evidence.terminal_event = data.type;
      if (typeof data?.response?.id === "string" && /^[A-Za-z0-9_-]{1,160}$/.test(data.response.id))
        evidence.response_id = data.response.id;
      observeError(data?.error || data?.response?.error || (data?.type === "error" ? data : undefined), evidence, argsKey);
    },
    onResponse: async (response) => {
      timings.response_headers_ms = elapsed();
      await diagnostic("model.response.headers", {status:response.status, elapsed_ms:elapsed()});
      await emit({
        id,
        event: {
          type: "response",
          status: response.status,
          request_id: response.headers["x-request-id"],
        },
      });
    },
  };
  const stream = runtime.streamSimple(model, context, options);
  let emitted = 0,
    largestPartial = 0;
  for await (const event of stream) {
    if (event.type.endsWith("_delta") && event.delta?.length) {
      timings.first_content_ms ??= elapsed();
      if (event.type === "text_delta") timings.first_text_ms ??= elapsed();
    }
    if (event.type === "done" || event.type === "error")
      event.transport_timings = { ...timings, total_ms: elapsed() };
    if (event.type === "error") {
      evidence.output ||= event.error?.content?.some(
        (part) =>
          part.type === "toolCall" ||
          part.text?.length ||
          part.thinking?.length,
      );
      evidence.terminal = !signal.aborted;
      evidence.sdk_terminal = event.type;
      const problem = failure(event.error, evidence, argsKey);
      await diagnostic("model.request.failed", {failure:problem,elapsed_ms:elapsed(),transport_timings:event.transport_timings});
      event.error.errorMessage = problem.message;
      event.error.failure = problem;
      event.http_status = problem.status;
      return { terminal: event, failure: problem };
    }
    if (event.type === "done") {
      if (Buffer.byteLength(JSON.stringify(event)) > 8 * 1024 * 1024)
        throw new Error("native_response_exceeds_bound");
      evidence.terminal = true;
      await diagnostic("model.request.completed", {elapsed_ms:elapsed(),response_id:evidence.response_id,terminal_event:evidence.terminal_event,transport_timings:event.transport_timings});
      for (const bufferedEvent of buffered)
        await emit({ id, event: bufferedEvent });
      return { terminal: event };
    }
    if (
      !evidence.output &&
      Buffer.byteLength(JSON.stringify(event)) > 8 * 1024 * 1024
    )
      throw new Error("native_response_exceeds_bound");
    if (
      (event.type.endsWith("_delta") && event.delta?.length) ||
      event.type === "toolcall_start"
    ) {
      evidence.output = true;
      for (const bufferedEvent of buffered.splice(0))
        await emit({ id, event: bufferedEvent });
    }
    if (!evidence.output) {
      if (buffered.length >= 32)
        throw new Error("native_event_buffer_exceeded");
      buffered.push(structuredClone(event));
      continue;
    }
    if (args.inference && event.type !== "text_delta") continue;
    const forwarded =
      args.inference && event.type === "text_delta"
        ? { type: "text_delta" }
        : event;
    // Pi events repeat the whole growing assistant message as `partial`.
    // Bound each IPC frame and unique response data, not the quadratic sum
    // of identical prefixes (fine-grained providers can emit one char/event).
    const { partial, ...incremental } = forwarded;
    const partialBytes = partial
      ? Buffer.byteLength(JSON.stringify(partial))
      : 0;
    const incrementalBytes = Buffer.byteLength(JSON.stringify(incremental));
    if (partialBytes + incrementalBytes > 8 * 1024 * 1024)
      throw new Error("native_response_exceeds_bound");
    largestPartial = Math.max(largestPartial, partialBytes);
    emitted += incrementalBytes;
    if (emitted + largestPartial > 64 * 1024 * 1024)
      throw new Error("native_response_exceeds_bound");
    await emit({ id, event: forwarded });
  }
  throw new Error("native_stream_incomplete");
}

async function run(id, args, signal) {
  if (!args.retry) {
    const evidence = { submission: "not_submitted", output: false };
    const result = await attemptContext.run(evidence, () =>
      runAttempt(id, args, signal, evidence),
    );
    await emit({ id, event: result.terminal });
    return { finished: true };
  }
  const gate = (event) =>
    new Promise((resolve, reject) => {
      const permit = randomUUID();
      const abort = () => {
        permits.delete(permit);
        reject(new Error("native_cancelled"));
      };
      signal.addEventListener("abort", abort, { once: true });
      permits.set(permit, {
        id,
        done: (answer) => {
          permits.delete(permit);
          signal.removeEventListener("abort", abort);
          if (answer.error) reject(new Error("native_admission_failed"));
          else resolve(answer.result);
        },
      });
      if (signal.aborted) abort();
      else void emit({ id, event: { ...event, permit } });
    });
  const terminal = await reliableRequest({
    retryProviderRequest,
    gate,
    signal,
    policy: args.retry || { max_retries: 0 },
    secret: () => argsKey,
    attempt: (evidence) => runAttempt(id, args, signal, evidence),
    emit: (event) => emit({ id, event }),
  });
  await emit({ id, event: terminal });
  return { finished: true };
}

async function command(id, action, args, signal) {
  if (action === "providers")
    return providers.map((p) => ({
      id: p.id,
      name: p.name,
      auth: Object.keys(p.auth).map((a) => (a === "apiKey" ? "api_key" : a)),
      subscription: !!p.auth.oauth?.isSubscription,
    }));
  if (action === "describe") {
    const selected = providers.find((p) => p.id === args.provider);
    if (!selected) return null;
    const directory = ai.createModels({
      credentials: { read: async () => undefined },
      authContext: {
        env: async () => undefined,
        fileExists: async () => false,
      },
    });
    directory.setProvider(selected);
    const model = directory.getModel(args.provider, args.model);
    return model ? modelMetadata(model) : null;
  }
  if (action === "init") return init(args);
  if (!runtime) throw new Error("native_not_initialized");
  if (action === "status")
    return {
      connected: !!(await runtime.checkAuth(spec.provider)),
      provider: spec.provider,
      auth: spec.auth,
    };
  if (action === "logout") {
    await runtime.logout(spec.provider);
    return { connected: false };
  }
  if (action === "catalog")
    return runtime.getModels(spec.provider).map(modelMetadata);
  if (action === "validate") {
    const { model } = modelFor(args);
    return { max_output_tokens: model.maxTokens, api: model.api };
  }
  if (action === "stream") return run(id, args, signal);
  if (action === "login") {
    if (spec.key_env) throw new Error("native_account_uses_environment_key");
    const device = join(folder, "device-id");
    let deviceId;
    try {
      deviceId = (await readFile(device, "utf8")).trim();
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      deviceId = randomUUID();
      try {
        await writeFile(device, deviceId, { mode: 0o600, flag: "wx" });
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        deviceId = (await readFile(device, "utf8")).trim();
      }
    }
    await runtime.login(
      spec.provider,
      spec.auth,
      {
        signal,
        notify: (event) => {
          void emit({ id, event: { type: "auth", data: event } });
        },
        prompt: (prompt) =>
          new Promise((resolve, reject) => {
            const promptId = randomUUID();
            const abort = () => {
              prompts.delete(promptId);
              reject(new Error("native_login_cancelled"));
            };
            const done = (value) => {
              signal.removeEventListener("abort", abort);
              prompt.signal?.removeEventListener("abort", abort);
              resolve(value);
            };
            prompts.set(promptId, done);
            signal.addEventListener("abort", abort, { once: true });
            prompt.signal?.addEventListener("abort", abort, { once: true });
            void emit({
              id,
              event: {
                type: "prompt",
                prompt_id: promptId,
                data: { ...prompt, signal: undefined },
              },
            });
          }),
      },
      { getDeviceId: () => deviceId, agentName: "Personal Agent" },
    );
    return { connected: true, provider: spec.provider, auth: spec.auth };
  }
  throw new Error("native_unknown_action");
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    process.exitCode = 1;
    break;
  }
  if (request.action === "attempt_ack") {
    const permit = permits.get(request.permit);
    if (permit?.id === request.target) permit.done(request);
    continue;
  }
  if (request.action === "cancel") {
    active.get(request.target)?.abort();
    continue;
  }
  if (request.action === "answer") {
    const answer = prompts.get(request.prompt_id);
    prompts.delete(request.prompt_id);
    answer?.(request.value);
    continue;
  }
  const controller = new AbortController();
  active.set(request.id, controller);
  void command(
    request.id,
    request.action,
    request.arguments || {},
    controller.signal,
  )
    .then((result) => emit({ id: request.id, result }))
    .catch(() =>
      emit({ id: request.id, error: "native_" + request.action + "_failed" }),
    )
    .finally(() => {
      controller.abort();
      active.delete(request.id);
    });
}
for (const controller of active.values()) controller.abort();
await writing;
