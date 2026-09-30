# Security and privacy

Codex Token Dashboard is designed to stay on the local machine.

- The server binds to `127.0.0.1` by default.
- The scanner reads only Codex rollout metadata and reported token usage.
- Prompt text, assistant output, tool arguments, tool output, encrypted
  reasoning, and credentials are not stored in the dashboard database.
- `auth.json` and API keys are never read.
- The web interface contains no CDN, analytics, telemetry, or remote assets.

Do not bind the service to `0.0.0.0` on an untrusted network. If you need
remote access, put the service behind an authenticated reverse proxy and a
firewall.

To report a vulnerability, open a private security advisory in the GitHub
repository rather than a public issue.
