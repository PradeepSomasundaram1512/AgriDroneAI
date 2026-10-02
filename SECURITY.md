# Security

- **No secrets in the repo.** The only optional secret is `ANTHROPIC_API_KEY` (GitHub Actions secret). Hardware credentials never leave the ground station.
- **The cloud cannot actuate hardware.** The autopilot only appends to `state/queue.jsonl`; the farm ground station independently re-validates and needs `AGRIDRONE_ARMED=1` plus `hardware.enabled` in policy. See `docs/GROUND_STATION.md`.
- **Supply chain:** zero runtime dependencies (standard library only). CI runs `bandit` (medium+ fails the build) and `pip-audit` on the optional extras; Dependabot is enabled for pip and Actions.
- **Untrusted input:** policy is schema-validated on load; weather/network failures fall back safely; sensor readings are range/outlier/stuck-checked before use; state files are integrity-checked before every commit.
- **The LLM advisor is bounded:** it can only suggest threshold nudges within hard bounds (`advisor.py`), never flight commands.
- Report a vulnerability by opening a private security advisory on the repository.
