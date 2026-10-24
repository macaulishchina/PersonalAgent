# Local speech adapters

Platform: Linux only; Windows / Windows Server are not certified. See the [module index](../../../../docs/reference/modules.md).
Voice is currently disabled; adapter and device checks do not establish voice readiness.

These adapters implement the contracts in `api/speech.py` and are injected by the
composition root. They do not open microphones/speakers, create tasks or call cloud APIs.

`LocalRecognition` uses sherpa-onnx streaming bilingual Paraformer int8. The model
directory must contain encoder.int8.onnx, decoder.int8.onnx and tokens.txt. It uses a
single inference worker, two engine threads, PCM16 mono 16 kHz, explicit reset and bounded
utterance endpoints. Its text has no reliable word timestamps or calibrated intent
confidence. Trailing hesitation fragments are kept open; this is not a general semantic
end-of-turn model. TV speech remains a known limitation.

Model source and upstream usage:
https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-paraformer/paraformer-models.html

`LocalSpeech` uses the previously installed Kokoro multilingual int8 model. It generates
literal speech with an explicitly configured voice, at most 240 characters / 30 seconds,
then normalizes to PCM16 mono 24 kHz without extra gain. Only one synthesis job may be
outstanding. A timed-out native computation cannot be forcibly killed in a Python thread;
its result is fenced and the next render waits within its deadline for that work to finish. No unbounded
queue or paid fallback is created. Model loading/generation stays off the event loop.

Both are baseline adapters, not approved defaults. See the [interaction reference](../../core/speech/interaction/README.md) for actual
evaluation, replacement requirements, configuration and rollout gates.
