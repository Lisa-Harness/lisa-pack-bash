# Using bash_run

`bash_run` runs commands in a persistent, agent-optimized bash session.

## Behavior

- **Output demarcation** — each command's output is separated from the prompt
  by a unique per-session sentinel; the handler strips it, so `output_text` is
  clean stdout. The exit status is recovered into `return_code`.
- **Bounded output** — large outputs are truncated (`truncated: true`); the
  full command + complete output are always written to the per-session audit
  log at `log_path`, so nothing is lost.
- **Timeouts** — a command that exceeds `timeout_seconds` is killed; the
  session itself is unaffected.

## Sandboxing

When the `lisa-pack-bwrap` pack is installed and bubblewrap is available,
commands run inside a namespace sandbox (read-only root, network off by
default). The default backend is **ephemeral tmpfs**: any file the command
creates or modifies is **discarded when the run exits**.

- `sandboxed: true` + `backend` and `persistence` tell you where the command
  ran and whether its mutations were kept (`persistence: "ephemeral"` means
  they were not).
- `sandboxed: false` means the sandbox is not available and the command ran
  directly on the host. This is always reported, never hidden.

You do not need to request sandboxing; it is automatic. `network: true` opts a
single call into network access. Only use `persist: true` when you explicitly
need file mutations captured in an overlayfs layer — it requires unprivileged
overlayfs AND a bubblewrap build that preserves the child's mount capability,
which most do not; when it cannot run, the result carries `degraded: true`
and the mutations were still discarded. Prefer keeping each command's write
and read steps in the SAME `bash_run` call (write the file, then use it in
that one call), and re-create any file you need across calls.

## State

Nothing on the filesystem persists across `bash_run` calls: each call starts
from the host's real filesystem under a read-only view, and writes land in
ephemeral tmpfs that is thrown away. Treat every call as a fresh machine:
create and consume files within a single call, and re-create anything a later
call needs. The audit log at `log_path` is the only durable trace of a run.

## Logging

Every command and its complete output is appended to the session audit log at
`output_json.log_path`. Review it to see exactly what ran and what it produced.
