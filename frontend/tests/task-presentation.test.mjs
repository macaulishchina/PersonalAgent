import test from "node:test";
import assert from "node:assert/strict";
import { runLabel, businessUnsettled } from "../src/task-presentation.ts";

test("execution completion never claims an unknown business operation succeeded", () => {
  const run = {
    state: "completed",
    data: { business_result: { state: "unknown" } },
  };
  assert.equal(runLabel(run), "执行已结束 · 业务结果待核验");
  assert.equal(businessUnsettled(run), true);
  run.data.business_result.state = "waiting_user";
  assert.match(runLabel(run), /需要你处理/);
  run.data.business_result.state = "completed";
  assert.equal(runLabel(run), "本次已完成");
  assert.equal(businessUnsettled(run), false);
});

test("ordinary tasks preserve their labels and confirmed failures permit deliberate continuation", () => {
  assert.equal(runLabel({ state: "completed", data: {} }), "本次已完成");
  assert.equal(
    businessUnsettled({
      state: "failed",
      data: { business_result: { state: "failed" } },
    }),
    false,
  );
});
