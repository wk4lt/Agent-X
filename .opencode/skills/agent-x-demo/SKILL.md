---
name: agent-x-demo
description: Create a safe JSON inventory of the current Agent-X workspace for demonstrating Skill discovery and script execution.
compatibility: opencode
metadata:
  audience: developers
  purpose: harness-demo
---

# Agent-X Demo Skill

Use this Skill when demonstrating the Agent-X Skill flow.

1. Load this Skill only when the user asks to inspect the workspace.
2. When a structured inventory is useful, request `scripts/workspace_report.py` through
   `run_skill_script` with an optional relative workspace path argument.
3. The script is read-only: it reports file names, sizes, and a total count. It does not read
   file contents, execute shell commands, access environment variables, or make network requests.
