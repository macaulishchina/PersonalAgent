# 0.4.8 — durable scoped conversations and task tools

Historical release note. Current contracts are indexed in the [module guide](../reference/modules.md); the [0.4 release history](release-history-0.4.md) contains the other retained version notes.

- Public `conversation.create` accepts a caller-isolated UUID `request_id` and optional `policy` (`tools`, `instructions`, `capture_memory`). Reuse returns the original session; conflicting arguments fail. No caller may inspect another caller's creation receipt.
- `receipt` and batched `receipts` expose accepted request state within a session, including after a lost acknowledgment.
- Session policy restricts both fresh and resumed Pi tool declarations. Sessions with `capture_memory=false` are omitted from automatic global memory capture.
- Tasks accept optional `tools`; child restrictions inherit and may narrow, never expand. Background `execution_info` is limited to the caller's own execution.

These are generic PA capabilities. KFC issue ownership, permissions, material versions, outbox and conclusion rules remain in KFC.
