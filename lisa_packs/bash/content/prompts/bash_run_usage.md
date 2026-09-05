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
default) with a transparent overlayfs layer capturing mutations.

- `sandboxed: true` + `backend`, `layer_id`, `persistence` tell you where the
  command ran and whether its file mutations are kept.
- `sandboxed: false` means the sandbox is not available and the command ran
  directly on the host. This is always reported, never hidden.

You do not need to request sandboxing; it is automatic. To override for a
specific call, use `persist`, `layer_id`, or `network`.

## State

cwd, exported environment variables, and files persist across `bash_run` calls
within a session (in the sandbox, under the session's overlay layer). Re-declare
anything you depend on to be explicit and reproducible.

## Logging

Every command and its complete output is appended to the session audit log at
`output_json.log_path`. Review it to see exactly what ran and what it produced.
