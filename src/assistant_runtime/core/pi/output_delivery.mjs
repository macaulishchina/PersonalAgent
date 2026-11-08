// Optional presentation evidence. Never changes provider messages, tool calls,
// transcript signatures, sampling parameters or retry eligibility.
const phases = new Map([["commentary", "commentary"], ["final_answer", "final"]]);

export class OutputPhases {
  constructor() {
    this.items = new Map();
    this.pending = [];
    this.blocks = new Map();
  }

  observe(event) {
    if (!["response.output_item.added", "response.output_item.done"].includes(event?.type)) return;
    const item = event.item;
    if (item?.type !== "message" || typeof item.id !== "string") return;
    let entry = this.items.get(item.id);
    if (!entry) {
      if (this.items.size >= 128) return; // Presentation metadata is optional, content is not.
      entry = {};
      this.items.set(item.id, entry);
      this.pending.push(entry);
    }
    const phase = phases.get(item.phase);
    if (phase) {
      if (entry.phase && entry.phase !== phase) {
        // A provider contradicted its own classification. Do not choose one.
        entry.conflict = true;
        delete entry.phase;
      } else if (!entry.conflict) entry.phase = phase;
    }
  }

  decorate(event) {
    if (!event?.type?.startsWith("text_") || !Number.isInteger(event.contentIndex)) return event;
    if (event.type === "text_start") this.blocks.set(event.contentIndex, this.pending.shift() || {});
    const entry = this.blocks.get(event.contentIndex);
    if (entry?.conflict) return { ...event, delivery: { phase: "unknown", evidence: "conflict", block: event.contentIndex } };
    if (!entry?.phase) return event;
    return { ...event, delivery: { phase: entry.phase, evidence: "provider", block: event.contentIndex } };
  }
}

export function registerOutputDelivery(pi) {
  let emitted = new Map();
  pi.on("message_start", (event) => {
    if (event.message?.role === "assistant") emitted = new Map();
  });
  pi.on("message_update", (event, ctx) => {
    const value = event.assistantMessageEvent?.delivery;
    if (!value || !(["final", "commentary"].includes(value.phase) && value.evidence === "provider" ||
                     value.phase === "unknown" && value.evidence === "conflict") ||
        !Number.isInteger(value.block) || value.block < 0 || value.block >= 128 ||
        !["provider", "conflict"].includes(value.evidence)) return;
    if (emitted.get(value.block) === value.phase) return;
    emitted.set(value.block, value.phase);
    // The existing RPC notification channel keeps this metadata out of the
    // model transcript. Only the framework adapter consumes the reserved tag.
    ctx.ui.notify("__assistant_message_delivery__:" + JSON.stringify(value), "info");
  });
}
