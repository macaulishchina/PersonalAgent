import test from "node:test";
import assert from "node:assert/strict";
import {
  mergeTrace,
  traceSteps,
  stepState,
  stepTitle,
  stepSeconds,
} from "../src/task-trace-model.ts";

const event = (seq, call_id, phase, rest = {}) => ({
  seq,
  call_id,
  phase,
  at: seq,
  kind: "mcp",
  server: "media",
  tool: "lookup",
  status: phase === "started" ? "running" : "completed",
  ...rest,
});

test("pages and replay merge by event identity; phases become one call", () => {
  const first = event(1, "turn1:call", "started");
  const end = event(4, "turn1:call", "completed");
  const rows = mergeTrace(
    [first, event(2, "turn2:call", "started")],
    [end, first],
  );
  const steps = traceSteps(rows);
  assert.equal(rows.length, 3);
  assert.equal(steps.length, 2);
  assert.equal(stepState(steps[0], "running").label, "调用完成");
  assert.equal(stepSeconds(steps[0]), 3);
  assert.equal(stepState(steps[1], "running").label, "调用中");
});

test("failed and unconfirmed tools never look successful", () => {
  const steps = traceSteps([
    event(1, "failed", "completed", { status: "failed" }),
    event(2, "missing", "completed", { status: "unknown" }),
    event(3, "interrupted", "started"),
  ]);
  assert.equal(stepState(steps[0], "completed").tone, "error");
  assert.equal(stepState(steps[1], "completed").tone, "unknown");
  assert.equal(stepState(steps[2], "completed").label, "未收到结束记录");
  assert.equal(stepSeconds(steps[0]), null);
});

test("out of order arrival keeps completed phase and avoids negative duration", () => {
  const [step] = traceSteps([
    event(1, "call", "completed"),
    event(2, "call", "started"),
  ]);
  assert.equal(stepState(step, "running").label, "调用完成");
  assert.equal(stepSeconds(step), null);
});

test("labels preserve unknown tools and bounded data", () => {
  assert.equal(stepTitle(traceSteps([event(1, "a", "started")])[0]), "lookup");
  assert.equal(
    stepTitle(
      traceSteps([event(1, "a", "started", { kind: "command" })])[0],
    ),
    "执行命令",
  );
  const items = Array.from({ length: 510 }, (_, i) =>
    event(i + 1, String(i), "started"),
  );
  assert.equal(mergeTrace([], items).length, 500);
});
