import test from "node:test";
import assert from "node:assert/strict";
import { conversationBase } from "../src/http.ts";

test("conversation routes bind a whole page and never reinterpret malformed paths", () => {
  const id = "a".repeat(32);
  assert.equal(conversationBase(`/chat/c/${id}/`), `/chat/c/${id}/`);
  assert.equal(
    conversationBase(`/chat/e/${id}/account/`),
    `/chat/e/${id}/account/`,
  );
  assert.equal(
    conversationBase(`/chat/e/${id}/visit/`),
    `/chat/e/${id}/visit/`,
  );
  for (const path of [
    "/",
    "/chat/",
    "/chat/link",
    `/chat/c/${id}/extra`,
    "/chat/c/admin/",
    `//chat/c/${id}/`,
  ]) {
    assert.equal(conversationBase(path), "/chat/");
  }
});
