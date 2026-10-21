import test from "node:test";
import assert from "node:assert/strict";
import { groupMessages } from "../src/message-presentation.ts";

const report = (id, overrides = {}) => ({
  id,
  role: "assistant",
  kind: "notification",
  source: "notification",
  turn_id: `notification:${id}`,
  updated: 100,
  text: "资源已转存",
  report_group: {
    id: "group",
    title: "任务",
    task_id: "task",
    run_id: "run",
    important: false,
  },
  ...overrides,
});

test("consecutive reports collapse without dropping details or counting completion", () => {
  const messages = Array.from({ length: 20 }, (_, i) => report(String(i)));
  messages[3].report_group.important = true;
  const result = groupMessages(messages);
  assert.equal(result.length, 1);
  assert.deepEqual(result[0].messages, messages);
  assert.equal(result[0].id, "0");
});

test("same title never crosses execution, group, task, or unknown association", () => {
  const first = report("a");
  for (const delta of [
    { run_id: "next" },
    { id: "other" },
    { task_id: "other" },
  ]) {
    const second = report("b", {
      report_group: { ...first.report_group, ...delta },
    });
    assert.equal(groupMessages([first, second]).length, 2);
  }
  assert.equal(
    groupMessages([
      report("a", { report_group: undefined }),
      report("b", { report_group: undefined }),
    ]).length,
    2,
  );
});

test("intervening conversation preserves chronology, including a report from another task", () => {
  for (const middle of [
    report("user", { role: "user", kind: "message" }),
    report("reply", { kind: "message" }),
    report("other", {
      report_group: { ...report("x").report_group, id: "other" },
    }),
  ]) {
    assert.equal(groupMessages([report("a"), middle, report("b")]).length, 3);
  }
});

test("loaded history and corrected reports regroup deterministically without mutating inputs", () => {
  const old = report("old");
  const current = report("current");
  const before = JSON.stringify([old, current]);
  assert.equal(groupMessages([current])[0].messages.length, 1);
  assert.equal(groupMessages([old, current])[0].messages.length, 2);
  assert.equal(JSON.stringify([old, current]), before);
  assert.equal(
    groupMessages([
      old,
      {
        ...current,
        text: "核验失败",
        report_group: { ...current.report_group, important: true },
      },
    ])[0].messages[1].text,
    "核验失败",
  );
});

test("normal reply tools still group by turn and voice remains separate", () => {
  const a = report("a", { kind: "message", source: "web", turn_id: "turn" });
  const b = report("b", { kind: "detail", source: "web", turn_id: "turn" });
  assert.equal(groupMessages([a, b]).length, 1);
  assert.equal(groupMessages([a, { ...b, source: "voice" }]).length, 2);
});
