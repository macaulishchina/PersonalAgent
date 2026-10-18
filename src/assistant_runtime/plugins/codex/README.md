# Codex voice and foreground execution

This adapter owns the signed-in subscription process, native WebRTC, foreground
execution and provider protocol. `conversation` owns output eligibility; `assistant`
owns local stop/sleep and attention. Background execution remains in `tasks`.

## Native protocol boundary

Inspect the **installed** experimental schema before assuming a native feature:

```sh
codex app-server generate-json-schema --experimental --out state/acceptance/voice-boundary/schema
```

The current installed protocol (0.162.1) exposes `clientManagedHandoffs` for response
forwarding, **not** approval before a voice delegation executes. `delegationAckFiller=false`
disables automatic delegation filler. Native speech can still generate acknowledgments.
`handoff_request` is a notification, not a client approval request. Do not claim that a
prompt or receiving that notification makes shell/third-party effects cancellable.
These provider names stay in this adapter, not kernel contracts.

WebRTC PCM arrives without a reliable per-answer ID. `appendSpeech` acceptance is
neither a playout receipt nor proof that a person heard it. It remains a generative
voice renderer; check actual final transcripts during real acceptance. Do not replace
this with an API-key service or silently relax the instance sandbox.

## Turn and output ownership

`turns.py` retains at most 32 foreground receipts. Each turn owns its task identity,
progress, bounded message deduplication and full bounded final report. Commentary is
not a final report. A new native turn never inherits old progress; late notifications
cannot finish another turn, and duplicate completions cannot deliver again. Overlapping
start/completion notifications update their original task rows independently.

`conversation/feedback.py` persists output bindings to session, voice epoch and input
revision. A new execution supersedes a pending old reply. Closed epochs, new user focus,
quiet hours, empty successful reports and interrupted replies do not produce a new
spoken completion. Provider failure without prose gets one factual failure notice.
Claim output before the RPC; a lost acknowledgement stays `unknown` and is never
automatically replayed. Restart closes old bindings and preserves uncertainty.

For voice execution, normalized provider input acceptance schedules one acknowledgement
after 0.8 seconds. A fast completion merges it into the result; commentary and tool
details never trigger speech. A native reply already associated with that input prevents
an extra acknowledgement. Accepted execution inputs renew the voice epoch/revision
binding when a follow-up steers the same foreground turn. Their IDs are durably
deduplicated, so replay cannot reclaim focus. The optional command entrance uses the
same receipt principle and a literal local renderer; see `interaction/README.md`.

Acknowledgement submission runs in owned work, outside the provider event pump. Only
result delivery waits behind it. Native speech drains after assistant transcript completion
through 500 ms of quiet PCM before restoring execution silence, with a 15-second total
bound; interruption, Web input, a new execution and closing release the wait immediately.
This is a transport heuristic, not a per-answer playout receipt. Failed/uncertain
acknowledgements are recorded without retry or cancelling execution. The actual final
report remains separately eligible for speech.

Eligible results use `appendSpeech`, instead of another developer prompt asking the
voice model to invent a completion response. Preserve error/output code blocks as
evidence. Long reports stay in the conversation instead of reading a truncated prefix
that might omit a later failure. Explicit request IDs and device receipts remain the
evidence of effects; finishing a provider turn is not proof the user's whole goal succeeded.

While a delegation/execution awaits a result, speculative native output is suppressed.
Only the matching execution can release its result output. A new actual voice input can
take the floor; late data-channel events cannot undo a local explicit mute. An uncertain
speech submission remains gated until fresh input/connection. Same-connection quiet-boundary
suppression is still a heuristic; only closing and rejecting the old connection generation
provides the strict exit fence.

## Short requests and exit

`voice_follow_up_seconds` defaults to 8 (1..30). After a spoken reply finishes, the
assistant returns to standby when that window expires. `陪我聊聊` explicitly selects
continuous conversation for that activation; the next wake starts short again.
`idle_seconds` bounds initial waiting, continuous idle and awaiting execution without
new input. Raw VAD and silent RTP never extend attention. A provider-accepted user
turn can still originate in a television: this phase does not implement HomePod-style
directed-speech recognition or claim to solve acoustic false triggers.

Standalone `退下` forms and existing explicit endings go through local sleep. Negated,
quoted and explanatory uses do not. Sleep clears local audio and invalidates the old
voice generation without canceling authorized background work. Main I/O bindings and
quiet hours remain unchanged. `closing` is distinct from standby: repeated exits coalesce,
and a new wake waits until the prior transport cleanup has completed. Verified scoped `我自己来` takeover, semantic correction
versions and directed-speech admission are subsequent phases; do not equate takeover
with merely ending voice or claim an unconfirmed device action has stopped.

## Verification

During development run `scripts/check.py --group core --group codex`; after final source
changes run the full check. Regression tests include empty/overlapping turns, delayed
completions, final-vs-commentary, output identity, duplicate/lost acknowledgements,
restart, quiet hours, local endings and short/continuous deadlines.

Explicit installed-host acceptance uses known **synthetic** audio with a maintenance
`run --no-mic` host and the configured real output. Do not mix room audio into injection:

```sh
.venv/bin/python scripts/verify_voice.py --config config.toml \
  --request /absolute/synthetic-question.wav --exit /absolute/synthetic-exit.wav \
  --chat /absolute/synthetic-chat.wav
```

The script verifies output writes, short exit, explicit continuous mode and no late
playback after an exit, without changing I/O or quiet policy. Restore normal service
capture afterward. Its private JSON receipts are not acoustic or far-field microphone
acceptance. Separately probe an isolated subscription thread with MCP disabled and
only a bounded harmless calculation; verify both success/failure transcripts against
actual execution results. A sandbox error must remain a failure, never spoken as a
calculated result. No raw room recordings are required or retained by this workflow.
