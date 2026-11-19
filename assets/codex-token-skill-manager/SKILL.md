---
name: codex-token-skill-manager
description: Inspect and manage the local Codex Token Dashboard skill library for Codex or Hermes when the user asks to list, install, disable, or otherwise manage skills through the dashboard.
---

# Manage the local skill library

Run `CodexTokenSkills` from the desktop installation (on Windows, `CodexTokenSkills.exe`), or `python skills_service.py` from the development source. Start the dashboard first. The CLI discovers the desktop's loopback port; use `--port` for a different local server.

Select the requested agent explicitly. Start with `--agent codex --op state` or `--agent hermes --op state`. To inspect a particular entry, send an object with the exact ID from state to `--op detail --payload-stdin`. Pass JSON through stdin, not command-line arguments.

Only add `--apply` after the user authorizes the specific change or network operation. Preview imports before applying them. Keep the module's own confirmation fields; do not fix or bypass a failed validation by writing directly to an agent's skill folders. If a mutation reports failure or times out, inspect `state` or `activity` before retrying. Never execute scripts from imported skills as part of management.
