---
mode_id: shell
mode_provider_id: lisa-pack-bash
mode_kind: action
description: "Shell execution for command-line tasks via a persistent bash session."
routing_intent_tags: [shell, execute, terminal]
routing_intent_keywords:
  shell: [shell, bash, command, terminal, run, execute, cli]
satisfies_intents: [shell, execute]
priority: 5
allowed_tool_ids: [bash_run]
---

Shell mode: run command-line tasks in a persistent, agent-optimized bash
session. Commands execute through `bash_run`; when the bwrap pack is installed
they are sandboxed automatically. Every command and its output is written to a
per-session audit log that the user can inspect.
