---
schema_version: 2
name: e2e-recovery
surface:
  type: personal_dm
  id: host-console
  memory_scope: personal
resources:
  packs:
    configuration: {}
    required:
      - lisa-pack-bash
      - lisa-pack-bwrap
      - lisa-pack-markdown
      - lisa-pack-personas
routing: { from: { pack: lisa-pack-core, session: default } }
execution:
  max_session_tokens: 1000000
  max_completion_tokens: 200000
  max_capability_calls: 100
---
# E2E recovery session

You are a running instance of the Lisa host under an automated end-to-end
test of error handling and recovery plumbing.

In this session you will dispatch deliberately malformed tool calls. The
failures are intentional: they exercise the recovery pipeline that records
interrupted dispatch work. Report outcomes factually. Do not attempt to fix
or re-run a malformed dispatch; the operator will issue explicit recovery
commands.
