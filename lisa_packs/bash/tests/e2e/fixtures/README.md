---
name: e2e-recovery
description: Fixture data for the bash/recovery e2e scenario.
---

Recovery scenario: no fixture data files required. The stall is triggered by
asking `bash_run` to run with structurally invalid `overrides` (a real,
deterministic tool-dispatch failure), which settles a `stalled` recovery
record with `retry_policy=confirm`. The operator then inspects and dismisses
it through the recovery pack's commands.
