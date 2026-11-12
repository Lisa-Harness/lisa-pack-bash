---
schema_version: 2
name: e2e-history
surface:
  type: personal_dm
  id: host-console
  memory_scope: personal
resources:
  packs:
    configuration:
      lisa-pack-bwrap:
        sandbox:
          floor:
            execution: sandbox
            persistence: direct-write
            paths:
              ${WORKSPACE}: rw
    required:
      - lisa-pack-bash
      - lisa-pack-bwrap
      - lisa-pack-markdown
      - lisa-pack-personas
providers:
  tools:
    web_search: e2etest-offline
routing: { from: { pack: lisa-pack-core, session: default } }
execution:
  max_session_tokens: 1000000
  max_completion_tokens: 200000
  max_capability_calls: 100
---
# E2E shell session

You are a running instance of the Lisa host under an automated end-to-end test
of shell history semantics. The workspace is the fixture repository at `${WORKSPACE}`.
