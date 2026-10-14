# Personal Agent engineering guide

- Read `ARCHITECTURE.md` before changing a boundary. Each module owns one responsibility.
- `kernel/` imports only the standard library and its contracts. Never import plugins there.
- Keep Codex protocol fields inside `plugins/codex/`; native voice and task delegation share one owner.
- Add a capability contract and an adapter for a new device/provider. Do not add hardware branches to the assistant.
- Before changing go2rtc deployment or adding cameras through it, read `deploy/go2rtc.md` for device mapping, stable identity, permissions, reconnection and acceptance.
- No paid API fallback, raw recording by default, automatic room fallback, or extra audio gain.
- Wake word, assistant display name, output names and hardware identities are instance configuration.
- Event notifications must not wait for device reconnect, model inference, or memory retrieval.
- Track background work and resource cleanup through plugin contexts. Never replay uncertain user tasks.
- Memory uses upstream Mem0, driven by the official Codex subscription task interface with local embeddings. Do not create a second live vector database, HTTP model proxy, or custom extraction/dedup/ranking algorithm.
- Before changing memory capture, test source validation, corrections, deletion, stale jobs and retries.
- A passing fake test does not establish real-device success. Record integration evidence separately.
- Run `.venv/bin/python scripts/check.py` before deployment. All code uses one `.venv`.
- Private data lives in ignored `state/`, `models/`, and `config.toml`. Never commit credentials or recordings.
- Development has one current API/CLI. Do not add compatibility aliases; Git is the rollback mechanism.
- Keep `scripts/` for reusable setup, checks and hardware acceptance. One-off probes/experiments belong in ignored instance storage or `/tmp`, not the source tree. Generate a report only when requested by the user.
