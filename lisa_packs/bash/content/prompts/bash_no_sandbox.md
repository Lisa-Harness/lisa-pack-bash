# Shell is running without a sandbox

The `bash_run` shell works, but the bubblewrap sandbox pack
(`lisa-pack-bwrap`) is not installed, so commands are running **directly on the
host** with no namespace isolation. Every result carries `sandboxed: false` so
this is visible to you and to the user.

## What sandboxing adds

- Read-only root filesystem; commands cannot mutate the real FS.
- Network isolation (off by default, opt-in per command).
- Ephemeral write space: command mutations go to tmpfs inside the sandbox and
  are discarded on exit (the real FS stays untouched).

## How to enable it

Install the `lisa-pack-bwrap` pack and ensure the host has bubblewrap
(`bwrap`) and unprivileged user namespaces enabled. See `sandboxing_degraded.md`
for platform-specific install and kernel notes. No configuration is needed once
the pack is present - `bash_run` detects it and composes automatically.
