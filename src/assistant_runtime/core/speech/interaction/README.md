# Speech interaction (disabled)

The current application does not assemble the local command/ASR/TTS pipeline.
`app/bootstrap.py` leaves `state.interaction = None` and binds `DisabledVoice`;
setting `[interaction].enabled = true` does not enable a working voice entrance.
Use the CLI or configured chat webpage for conversation.

The modules in this directory retain development contracts for buffering,
recognition, routing and confirmation. They are not a supported deployment path.
Ordinary conversation and task execution continue through Pi; these components do
not add another agent loop or grant device permissions.

## Current boundary

- No microphone capture or successful speech playback is claimed by the disabled
  voice adapter. Device audio tools remain separate from the main voice entrance.
- Historical ASR/TTS settings and pipeline diagrams describe the frozen candidate,
  not configuration steps for the current composition.
- Enabling voice requires code integration and independent acoustic, latency,
  cancellation and device acceptance. Passing offline tests alone is insufficient.

See [architecture](../../../../../docs/development/architecture.md) and the
[speech plugin](../../../plugins/speech/README.md) for the active boundaries.

## Historical design and evidence

The complete prior design, configuration candidate, protocol assessment and
2026-10-18 measurements are preserved in the
[voice interaction archive](../../../../../docs/archive/voice-interaction-2026-10.md).
The measured limitations and unverified cases remain unchanged.

Offline development checks are documented in that archive and
[development testing](../../../../../docs/development/testing.md). They require
the stated local model/recording assets and do not establish a deployable voice path.
