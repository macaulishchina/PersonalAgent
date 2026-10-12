# Personal Agent engineering guide

- Read `ARCHITECTURE.md` before changing a boundary. Each module owns one responsibility.
- `kernel/` imports only the standard library and its contracts. Never import plugins there.
- Keep Codex protocol fields inside `plugins/codex/`; native voice and task delegation share one owner.
- Add a capability contract and an adapter for a new device/provider. Do not add hardware branches to the assistant.
- No paid API fallback, raw recording by default, automatic room fallback, or extra audio gain.
- Wake word, assistant display name, output names and hardware identities are instance configuration.
- Event notifications must not wait for device reconnect, model inference, or memory retrieval.
- Track background work and resource cleanup through plugin contexts. Never replay uncertain user tasks.
- Memory uses the upstream Basic Memory adapter. Do not create a second vector database or custom ranking algorithm.
- Before changing memory capture, test source validation, corrections, deletion, stale jobs and retries.
- A passing fake test does not establish real-device success. Record integration evidence separately.
- Run `.venv/bin/python scripts/check.py` before deployment. All code uses one `.venv`.
- Private data lives in ignored `state/`, `models/`, and `config.toml`. Never commit credentials or recordings.
- Development has one current API/CLI. Do not add compatibility aliases; Git is the rollback mechanism.
