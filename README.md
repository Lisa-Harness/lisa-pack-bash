# lisa-pack-bash

Persistent, agent-optimized shell execution for Lisa. This pack is the
model-facing shell surface: one tool, `bash_run`, backed by a per-session
primed bash that absorbs mechanical complexity so the model can stay on the
task instead of fighting the shell.

> Sandboxing (bubblewrap namespace isolation + overlayfs capture) is provided
> by the sibling pack [`lisa-pack-bwrap`](../bwrap/README.md). When bwrap is
> installed, `bash_run` composes through it automatically; this pack works
> without it and reports `sandboxed: false` loudly rather than silently.

## What the pack provides

| Kind | Id | Role |
| --- | --- | --- |
| Tool | `bash_run` | Run one command, a background job, or a batch of independent commands in a persistent bash session. Approval-gated (`approval_required: true`) — the user-facing boundary. |
| Mode | `shell` | Action mode routed by `/shell …` (or natural-language shell intent). Model-facing instructions + posture guidance for every execution regime. |
| Operation | `sandbox_facts` | Pre-turn op (bound via `pre_turn_operation_ids`) that resolves the session and publishes the per-session sandbox-containment fact for the host approval engine. Prevents the first-call "gate before session exists" inversion. |

`bash_run` is deliberately the **only** tool. The single-tool surface keeps
the model's cognitive load low; behavior is steered by parameters, pack
configuration, and the result JSON rather than by a spread of tools.

## Quick start

```bash
# Editable install from the packs-wip super-repo
pip install -e ./bash
```

Requires Python ≥ 3.11 plus `lisa-harness-kernel` and `lisa-harness-sdk`. `bash` must be on
`PATH` (a hard dependency: there is no shell without it — a missing bash is a
hard error carrying `missing: ["bash"]` and an install doc).

## Execution postures

Every `bash_run` result stamps its posture so neither the model nor the user
has to guess. The four regimes:

| Result stamp | Meaning | Guidance |
| --- | --- | --- |
| `sandboxed: false` (no `mode`) | **Unsandboxed.** bwrap pack not installed / sandbox degraded. Changes are permanent and real. | Prefer read-only first; explain+confirm mutating commands. |
| `sandboxed: false`, `mode: "bare"` | **Deliberate bare.** The harness floor *permits* host execution by choice, not breakage (no `missing`). | Same care as unsandboxed; tighten with `overrides` when no host write is needed. |
| `backend: "tmpfs"`, `persistence: "ephemeral"` | **Ephemeral sandbox.** Workspace bound read-only at its real path; writes go to a per-session host-visible `scratch_dir` and are discarded. | Probe freely; produce a unified diff/script under `scratch_dir` for the user. |
| `backend: "overlay"` | **Captured sandbox.** Opt-in (`persist`, `layer_id`, or a `capture` path override); writes land in an overlayfs layer at `capture_root` (and per `captured_paths` target) — a proposal, not an applied fix. | Report `layer_id` + `captured_paths`; review with the bwrap pack's `layers_export` op; the user applies via `layers_apply`. |

`persistence: "session"` drops the layer at session end;
`persistence: "persistent"` (with `persist: true`) retains it. Sandbox
availability is detected by *pack presence* (is `bwrap_run` registered), then
re-probed at runtime — a registered-but-broken bwrap (e.g. binary absent)
returns every error with a `missing` list and a remediation doc rather than a
bare stamp.

### File ownership inside the sandbox (`identity`)

Every sandboxed result also carries the bwrap pack's `identity` block
(forwarded beside `capture_root`/`scratch_dir`). The sandbox runs as **mapped
root** via a single-entry uid map (host user → sandbox uid 0), which means:

- the model's own files display as `root:root` inside the sandbox;
- files owned by any **other** host uid — including real root's — display as
  `nobody:nobody` (overflow uid 65534): a display artifact of the namespace,
  not a permissions change;
- on-disk ownership is unchanged, and overlay-captured files are owned by the
  real host user outside the sandbox.

The shell mode doc (`modes/shell/_mode.md`, "Ownership inside the sandbox")
teaches the rules that follow from this: check `sandboxed` before treating
owner columns as meaningful, prefer numeric ids (`ls -n`, `stat -c %u:%g`)
when ownership matters, and **never `chown`/`chmod` to "repair" owners inside
the sandbox** — ephemeral writes are discarded and overlay capture would
pollute the layer with bogus ownership metadata. Full mechanics in the bwrap
pack's `content/prompts/sandbox_ownership.md`.

## `bash_run` parameters

Exactly one of `command` / `commands` is required.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `command` | string | — | The shell command to run. Mutually exclusive with `commands`. |
| `commands` | list[string] | — | Independent commands run as one batch, capped at 8. Best for independent read-only fan-outs; not valid with `run_in_background`. |
| `when` | `"auto" \| "parallel" \| "serial"` | `"auto"` | Batch schedule. `auto` parallelizes in bare/ephemeral/readonly, serializes under overlay. Overlay capture **always** serializes, even if `parallel` is requested. |
| `timeout_seconds` | int | `30` | 1–300. A foreground timeout auto-backgrounds the work (see below) instead of killing it. |
| `run_in_background` | bool | `false` | Return a `job_id` immediately for long builds/tests/servers. Not valid with `commands`. |
| `persist` | bool | `false` | Only when sandboxed: keep the captured overlay layer past the session. |
| `layer_id` | string | `""` | Only when sandboxed: opt in to overlay capture with an explicit layer id. |
| `overrides` | object | `{}` | Only when sandboxed. **Tightening-only** profile overrides: `persistence` (`ephemeral`\|`overlay`\|`readonly`), `network` (`allow`\|`deny`), `paths` (`{path: rw|ro|hidden}`). Overrides may only raise integrity above the harness floor; asking to loosen is a policy error. |

## The session: what carries across calls

Each Lisa conversation identity maps to one `BashSession`
(keyed by a hash of workspace/user/plan/surface). Every call is a separate
`bash -c` process, but state is replayed through a host-written state file,
so the model experiences a persistent shell:

- **cwd carries** — `cd` once, later commands see it.
- **Exported env carries** — `export` once, later commands see it. Deltas are
  computed against the rc baseline so rc-managed exports (`LANG`, `TERM`,
  `NO_COLOR`, …) are never replayed.
- **Function bodies can carry** (opt-in): captured as base64 per function and
  replayed under a `# === lisa function replay ===` section of the state
  file, per `session_state` pack config (off by default).

The state file is written only by the host, between calls — a function body
containing `cd` can never corrupt cwd adoption (cwd lives above the replay
section marker; `apply_cwd` never edits at or below it).

### Priming (the rc)

Per session, `priming.py` renders `content/profile/lisa.rc.tmpl` with a unique
sentinel + log/state paths, then `session.py` builds an inner argv that
sources the rc explicitly (`bash -c` ignores `--rcfile`). The rc:

- disables history / history expansion (`set +H`, `HISTSIZE=0`) for determinism;
- pins `LANG`/`LC_ALL`/`LC_CTYPE` to `C.UTF-8` for machine-parseable output;
- sets `TERM=dumb`, `NO_COLOR=1`, `PAGER=GIT_PAGER=MANPAGER=cat` — no color,
  no pagers, no prompt surprises;
- installs a unique per-session sentinel as `PS1` and a `PROMPT_COMMAND` rc
  emitter (interactive fallback only);
- sources the state file to replay carried-over state;
- exports `LISA_SESSION_LOG` (the audit trail path).

After the command, the wrapper captures `$?` and `${PIPESTATUS[@]}` in a
single atomic assignment (assigning `$?` first would clobber PIPESTATUS), then
emits one `sentinel…rc=N pipe=…` line plus per-line state markers
(`state=cwd=`, `var NAME=`, `func NAME <b64>`) for the parser. The marker
carries a leading `\n` so output without a trailing newline cannot corrupt
the parse, and the executor falls back to `process.returncode` when no marker
parses — never hardcoding 0.

## Result shaping (trust the JSON, not folklore)

- **Exit status is authoritative** — `return_code`, except the benign-rc=1
  cases below. Never report success without checking.
- **Empty `rc=0` is explicit**: a `shaping_note` says the command produced no
  output. That's success, not a hung tool — don't retry.
- **Benign `rc=1`**: `grep`, `rg`, `egrep`, `fgrep`, `find`, `diff`, `test`,
  `[`, `cmp`, and `git diff`/`git grep` prefixes returning 1 is informational
  (e.g. no matches), surfaced as a `shaping_note`. Other nonzero codes are
  real failures — read `stderr` first.
- **PIPESTATUS is reported** via `pipe_statuses`; if an early pipeline stage
  failed while the last stage succeeded, a `shaping_note` explains. The
  overall rc stays the last stage's.
- **Large output spills to file**: `truncated: true` plus `stdout_path` /
  `stderr_path` (and byte counts) means the full stream is on disk
  (64 MiB spill cap; 512 KiB inline cap). Inspect the file with
  `head`/`tail`/`grep` instead of re-running.
- **Audit log**: every command and its output is appended to the per-session
  log at `log_path` (tee-by-default, host-side).

### Batches

`commands: [...]` runs up to 8 independent commands as one call. Batch
children share a snapshot of session state; env/function deltas are
discarded, and cwd is adopted only if every child agrees. Results come back
as a `results` list plus aggregate `return_code`, with `batch: true`,
`when`, and `requested_when` recorded. Use in-shell `&` / `wait` /
`xargs -P` for data-parallel work *within* one command instead.

## Background jobs

- Set `run_in_background: true` for long-running work; the result is
  `backgrounded: true` with `job_id`, `log_path`, `rc_path`, `status_path`,
  `status: "running"`.
- **A foreground command that hits `timeout_seconds` is auto-backgrounded the
  same way** (bare and sandboxed alike) — work is never silently lost —
  flagged by `auto_backgrounded_on_timeout: true`. Exception: commands
  starting with `sleep` or containing `git` keep the bounded path and are
  killed on timeout.
- Sandboxed auto-background races the model's timeout against an extended
  bwrap task (up to +600 s, capped at 3600 s) and adopts the in-flight task
  as a job on timeout, so the namespace stays alive until the command
  finishes; overlay capture completes normally. (`bwrap --die-with-parent`
  ties the child to this host process only.)
- Poll `rc_path` (integer exit code when done) and `status_path`
  (`done`/`killed`). Every later `bash_run` result includes `background_jobs`
  for still-running work. There is no separate listing/output tool.
- Job artifacts live at `<data_dir>/jobs/<session_key>/<id>.{log,rc,status.json}`.
  Jobs do **not** survive host-process restart.

## Streaming

`stream_output: true` is declared on the tool: as chunks arrive, the
executor's `StreamTap` forwards complete lines to the operator surface on the
`lisa.tool_output` bus topic (pathological single lines are truncated for the
view; the full text still reaches the model). Publishing is best-effort and
never breaks the call.

## Pack configuration

Read from the harness-owned session `resources.packs.configuration` as
`ctx.pack_config` — model tool parameters **cannot** forge it.

| Key | Shape | Effect |
| --- | --- | --- |
| `workspace_root` | path string | Workspace the session operates on (same channel as the coding pack). Forwarded into the child bwrap context when composing, so overlay capture/scratch target the real workspace — not `~/.lisa`. Falls back to `cwd` then `data_dir`. |
| `session_state` | `{cwd: bool, env: bool, functions: "off"\|"allowlist"\|"blocklist", patterns: [fnmatch…]}` | State-carryover policy. cwd and env default on; functions default `off`. `allowlist` replays only functions matching a `patterns` glob; `blocklist` replays everything but. Unknown keys ignored (stable extension point). |
| `sandbox.floor` | mapping | The harness containment floor. `floor.execution == "sandbox"` mandates sandboxing (a missing/degraded bwrap is then a hard error, `SandboxRequiredButUnavailable`); `== "bare"` deliberately permits host execution (stamps `mode: "bare"`). Other axes (`persistence`, `network`, `device`, `ipc`, `paths`, `tmpfs_budget_bytes`, `timeout_ceiling_seconds`) are passed through to the bwrap policy engine. |

## Content prompts surfaced to the model

Authored text under `content/prompts/`, attached to results as `doc` on the
matching error/degraded path — never silently swallowed:

- `bash_missing.md` — bash not on PATH (hard error).
- `bash_no_sandbox.md` — bwrap pack absent; running unsandboxed, how to enable.
- `bwrap_missing.md` — pack registered but host prerequisites missing.

## Relationship to `lisa-pack-bwrap`

`bash_run` is the **approval boundary**; `bwrap_run` is the sandbox backend it
composes through. The handoff is a tool-to-tool passthrough: the executor
builds a child `ToolExecutionContext` (forwarding `pack_config` so workspace
resolution and the floor keep working) and calls `bwrap_run`. Approval stays
on `bash_run` (`approval_required: true`); `bwrap_run` declares
`approval_required: false` and derives containment entirely from the harness
floor. See the [bwrap README](../bwrap/README.md) for the containment lattice,
backends, and layers.

## Development

```bash
cd bash
/home/adam/Code/lisa/.venv/bin/python -m pytest -p no:cacheprovider
```

Tests live in `lisa_packs/bash/tests/`. The `live` marker runs a real bash
session and exercises sandbox composition; run `-m "not live"` and `-m live`
separately. Entry point: `lisa.extensions.content_roots` →
`lisa_packs.bash:get_content_root`.
