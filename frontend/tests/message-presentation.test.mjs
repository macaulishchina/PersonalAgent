import test from "node:test";
import assert from "node:assert/strict";
import {
  groupMessages,
  messageChannel,
  needsAttention,
  replyMedia,
} from "../src/message-presentation.ts";

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

test("empty provider drafts do not multiply failure notices or hide successful tools and media", () => {
  const message = (id, extra = {}) => ({
    id,
    role: "assistant",
    kind: "message",
    source: "task",
    turn_id: "turn",
    updated: 100,
    text: "",
    status: "failed",
    ...extra,
  });
  const input = [
    message("blank-before-tools"),
    message("first-image", {
      kind: "detail",
      status: "complete",
      attachments: [{ id: "a" }],
    }),
    message("blank-between-tools", { text: "\n " }),
    message("second-image", {
      kind: "detail",
      status: "complete",
      attachments: [{ id: "b" }],
    }),
    message("third-image", {
      kind: "detail",
      text: "获取画面",
      status: "failed",
    }),
    message("blank-after-tools"),
    message("outcome", {
      text: "本轮回复失败，未自动重试。",
      status: "complete",
    }),
  ];
  const before = JSON.stringify(input);
  const visible = groupMessages(input).flatMap((group) => group.messages);
  assert.deepEqual(
    visible.map((m) => m.id),
    ["first-image", "second-image", "third-image", "outcome"],
  );
  assert.equal(visible.filter((m) => m.kind === "message").length, 1);
  assert.equal(visible.find((m) => m.id === "third-image").status, "failed");
  assert.equal(JSON.stringify(input), before);
});

test("only empty assistant replies are hidden; real partial content and other message kinds remain", () => {
  const message = (id, extra = {}) => ({
    id,
    role: "assistant",
    kind: "message",
    source: "task",
    updated: 100,
    text: "",
    ...extra,
  });
  for (const status of [
    "streaming",
    "complete",
    "failed",
    "unknown",
    "interrupted",
  ]) {
    assert.deepEqual(groupMessages([message("empty", { status })]), []);
    for (const extra of [
      { text: "部分回复" },
      { attachments: [{ id: "image" }] },
      { tool_summary: { kind: "create_task" } },
      { kind: "detail" },
      { kind: "notification" },
      { role: "user", attachments: [{ id: "input" }] },
    ]) {
      assert.equal(
        groupMessages([message("visible", { status, ...extra })]).length,
        1,
      );
    }
  }
});

test("consecutive reports collapse without dropping details or counting completion", () => {
  const messages = Array.from({ length: 20 }, (_, i) => report(String(i)));
  messages[3].report_group.important = true;
  const result = groupMessages(messages);
  assert.equal(result.length, 1);
  assert.deepEqual(result[0].messages, messages);
  assert.equal(result[0].id, groupMessages([messages[19]])[0].id);
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

test("interleaved reports join their execution across conversations and other tasks", () => {
  for (const middle of [
    report("user", { role: "user", kind: "message" }),
    report("reply", { kind: "message" }),
    report("other", {
      report_group: { ...report("x").report_group, id: "other" },
    }),
  ]) {
    const groups = groupMessages([
      report("a", { updated: 1 }),
      middle,
      report("b", { updated: 200 }),
    ]);
    assert.equal(groups.length, 2);
    assert.deepEqual(
      groups[1].messages.map((m) => m.id),
      ["a", "b"],
    );
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

test("one execution contains distinct purposes without hiding attention", () => {
  const first = report("attention", { task_id: "task" });
  first.report_group.kind = "attention";
  const second = report("result", { task_id: "task" });
  second.report_group.kind = "result";
  assert.equal(groupMessages([first, second]).length, 1);
  assert.equal(
    groupMessages([first, second])[0].messages.filter(needsAttention).length,
    1,
  );
  assert.equal(messageChannel(first), "attention");
  assert.equal(messageChannel(second), "report");
});

test("two concurrent tasks each form one collection, with all payloads intact", () => {
  const a = report("a", { updated: 1 });
  const b = report("b", {
    updated: 2,
    report_group: { ...a.report_group, id: "b", task_id: "b", run_id: "b" },
  });
  const c = report("c", { updated: 3 });
  const d = report("d", {
    updated: 4,
    report_group: { ...b.report_group, kind: "attention" },
  });
  const groups = groupMessages([a, b, c, d]);
  assert.deepEqual(
    groups.map((g) => g.messages.map((m) => m.id)),
    [
      ["a", "c"],
      ["b", "d"],
    ],
  );
  assert.equal(groups[0].id, groupMessages([c])[0].id);
  assert.equal(groups[1].messages[1], d);
});

test("a corrected early record moves its collection, never reorders ordinary dialogue", () => {
  const early = report("early", { updated: 10 });
  const user = report("user", { role: "user", kind: "message", updated: 20 });
  const reply = report("reply", { kind: "message", updated: 30 });
  const later = report("later", { updated: 40 });
  const finalUser = { ...user, id: "last", updated: 50 };
  const inputs = [early, user, reply, later, finalUser];
  const before = groupMessages(inputs);
  const corrected = { ...early, updated: 60, text: "更正后的结果" };
  const after = groupMessages([corrected, ...inputs.slice(1)]);
  assert.deepEqual(
    before.map((g) => g.messages.map((m) => m.id)),
    [["user"], ["reply"], ["early", "later"], ["last"]],
  );
  assert.deepEqual(
    after.map((g) => g.messages.map((m) => m.id)),
    [["user"], ["reply"], ["last"], ["later", "early"]],
  );
  assert.equal(before[2].id, after[3].id);
  assert.equal(inputs[0].updated, 10);
});

test("removing a report from between ordinary replies does not merge them", () => {
  const a = report("a", { kind: "message", turn_id: "same", updated: 10 });
  const b = { ...a, id: "b", updated: 30 };
  const groups = groupMessages([a, report("middle", { updated: 20 }), b]);
  assert.equal(groups.length, 3);
  assert.deepEqual(
    groups.map((g) => g.messages.map((m) => m.id)),
    [["a"], ["middle"], ["b"]],
  );
});

test("process sections follow recorded phase, preserve final replies and do not cross reports", async () => {
  const { replySections, replySettled } =
    await import("../src/message-presentation.ts");
  const message = (id, extra = {}) => ({
    id,
    role: "assistant",
    kind: "message",
    source: "web",
    turn_id: "one",
    text: "正文",
    ...extra,
  });
  const rows = [
    message("before", { presentation: { phase: "commentary" } }),
    message("tool", {
      kind: "detail",
      text: "调用工具\nlookup",
      status: "complete",
    }),
    message("final", { presentation: { phase: "final", body: "complete" } }),
    message("next-tool", {
      kind: "detail",
      text: "执行命令\nresult",
      status: "complete",
    }),
  ];
  const original = JSON.stringify(rows);
  assert.deepEqual(
    replySections(rows).map((s) => [s.process, s.messages.map((m) => m.id)]),
    [
      [true, ["before", "tool"]],
      [false, ["final"]],
      [true, ["next-tool"]],
    ],
  );
  assert.equal(
    replySettled(rows, "one", true),
    false,
    "completed tools are not a completed turn",
  );
  assert.equal(
    replySettled(rows, null, false),
    false,
    "missing state is not completion evidence",
  );
  assert.equal(
    replySettled(rows, "two", true),
    true,
    "a later active turn never keeps old process open",
  );
  assert.equal(
    replySettled(
      [
        ...rows,
        message("done", { presentation: { turn_status: "completed" } }),
      ],
      "one",
      true,
    ),
    true,
  );
  for (const status of ["failed", "interrupted", "unknown"]) {
    assert.equal(
      replySettled(
        [message("end", { presentation: { turn_status: status } })],
        "one",
        true,
      ),
      true,
    );
  }
  assert.equal(JSON.stringify(rows), original);
  assert.deepEqual(
    replySections([
      message("normal"),
      message("legacy-prose", { kind: "detail", text: "普通回复" }),
    ]).map((s) => s.process),
    [false, false],
  );
});

test("reply media follows its answer across process and notification groups without changing evidence", () => {
  const message = (id, extra = {}) => ({
    id,
    role: "assistant",
    kind: "detail",
    source: "task",
    turn_id: "turn",
    updated: 100,
    text: "调用工具\ncapture",
    presentation: { phase: "tool" },
    attachments: [],
    ...extra,
  });
  const image = { id: "image", name: "画面.jpg", type: "image/jpeg" };
  const video = { id: "video", name: "录像.mp4", type: "video/mp4" };
  const rows = [
    message("capture", { attachments: [image] }),
    report("notification", {
      updated: 101,
      attachments: [{ id: "report-image" }],
    }),
    message("capture-video", { updated: 102, attachments: [video] }),
    message("answer", {
      updated: 103,
      kind: "message",
      text: "已取得画面。",
      presentation: { phase: "final" },
      attachments: [image],
    }),
    message("other-turn", {
      updated: 104,
      turn_id: "other",
      attachments: [image],
    }),
  ];
  const original = JSON.stringify(rows);
  const groups = groupMessages(rows);
  const { galleries } = replyMedia(groups);
  const answer = groups.find((g) => g.messages.some((m) => m.id === "answer"));
  const first = groups.find((g) => g.messages.some((m) => m.id === "capture"));
  assert.deepEqual(galleries.get(answer.id), [image, video]);
  assert.equal((galleries.get(first.id) || []).length, 0);
  assert.deepEqual(galleries.get("other-turn"), [image]);
  assert.ok(
    ![...galleries.values()].flat().some((a) => a.id === "report-image"),
  );
  assert.equal(JSON.stringify(rows), original);
});

test("media remains visible while running or after failure, and late media joins the same reply", () => {
  const tool = {
    id: "tool",
    role: "assistant",
    source: "task",
    kind: "detail",
    turn_id: "turn",
    updated: 1,
    text: "调用工具\ncapture",
    attachments: [{ id: "a" }],
  };
  assert.deepEqual(replyMedia(groupMessages([tool])).galleries.get("tool"), [
    { id: "a" },
  ]);
  const failed = {
    ...tool,
    id: "failed",
    kind: "message",
    updated: 2,
    text: "后续请求失败，已取得的图片保留。",
    attachments: [],
  };
  const rows = [tool, failed];
  assert.deepEqual(replyMedia(groupMessages(rows)).galleries.get("tool"), [
    { id: "a" },
  ]);
  tool.attachments.push({ id: "late" });
  assert.deepEqual(replyMedia(groupMessages(rows)).galleries.get("tool"), [
    { id: "a" },
    { id: "late" },
  ]);
  assert.deepEqual(
    replyMedia(groupMessages(JSON.parse(JSON.stringify(rows)))),
    replyMedia(groupMessages(rows)),
  );
});

test("user media and messages without a turn identity never transfer into another reply", () => {
  const row = (id, role) => ({
    id,
    role,
    kind: "message",
    source: "web",
    text: "图片",
    updated: 1,
    attachments: [{ id }],
  });
  const { galleries } = replyMedia(
    groupMessages([
      row("input", "user"),
      row("reply", "assistant"),
      row("input2", "user"),
      row("reply2", "assistant"),
    ]),
  );
  assert.deepEqual([...galleries.keys()], ["reply", "reply2"]);
  assert.deepEqual(galleries.get("reply"), [{ id: "reply" }]);
  assert.deepEqual(galleries.get("reply2"), [{ id: "reply2" }]);
});

test("legacy attachments near a known turn share the gallery without granting inline placement", () => {
  const tool = {
    id: "old",
    role: "assistant",
    source: "task",
    kind: "detail",
    text: "调用工具\n旧记录",
    updated: 1,
    attachments: [{ id: "legacy" }],
  };
  const rows = [
    tool,
    {
      ...tool,
      id: "new",
      updated: 2,
      turn_id: "turn",
      attachments: [{ id: "registered" }],
    },
    {
      ...tool,
      id: "answer",
      updated: 3,
      turn_id: "turn",
      kind: "message",
      attachments: [],
    },
  ];
  const { galleries, byTurn } = replyMedia(groupMessages(rows));
  assert.deepEqual(galleries.get("old"), [
    { id: "legacy" },
    { id: "registered" },
  ]);
  assert.deepEqual(byTurn.get("turn"), new Set(["registered"]));
});
