# The bwrap sandbox pack is installed but cannot run on this host

The `lisa-pack-bwrap` pack is installed and `bash_run` is routing commands
through it, but the sandbox failed to execute. Commands CANNOT run silently on
the bare host: the pack fails the call and surfaces the reason instead. This is
deliberate - shell execution without the isolation the pack promises must never
be invisible.

## The cause

The error result carries a `missing` list with the exact host prerequisites
that are absent. Typically:

- **bubblewrap (bwrap) binary not found on PATH** — the pack is Python-only;
  `bwrap` is a host package it drives, not a dependency it can install.
- **unprivileged user namespaces are disabled** — the kernel refuses the
  sandbox's namespace creation.
- **bwrap is present but a namespaced run failed** — an LSM policy (SELinux /
  AppArmor) or hardened kernel is denying the mount/namespace syscalls.

## How to fix it

Install bubblewrap with the host package manager:

- **openSUSE**: `sudo zypper install bubblewrap`
- **Fedora / RHEL**: `sudo dnf install bubblewrap`
- **Debian / Ubuntu**: `sudo apt install bubblewrap`
- **Arch**: `sudo pacman -S bubblewrap`

If `bwrap` is installed but runs still fail, enable unprivileged user
namespaces (Debian/Ubuntu):

```bash
sudo sysctl kernel.unprivileged_userns_clone=1
echo 'kernel.unprivileged_userns_clone=1' | sudo tee /etc/sysctl.d/90-userns.conf
```

Until the host is fixed, tell the user plainly: shell commands cannot run
sandboxed, and `bash_run` is failing on purpose rather than falling back to
unsandboxed execution. Do NOT retry the same command in a loop.

## Fallback

If the user intentionally wants unsandboxed execution (their risk to take),
they can remove or disable the `lisa-pack-bwrap` pack; `bash_run` then runs on
the host directly and every result is stamped `sandboxed: false` (visible, not
silent).
