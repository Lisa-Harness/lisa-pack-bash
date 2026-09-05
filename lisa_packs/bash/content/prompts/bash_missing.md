# bash is not available

`bash_run` needs a bash interpreter on this host; bash is a hard dependency and
cannot be installed as a Python package.

## Install bash

- **Fedora / RHEL**: `sudo dnf install bash` (almost always already present)
- **Debian / Ubuntu**: `sudo apt install bash`
- **Arch**: `sudo pacman -S bash`
- **macOS**: `brew install bash` (or use the bundled `/bin/bash`)

## After installing

Ensure `bash` is on the `PATH` that Lisa's host process sees. Once bash is
available, `bash_run` will work without further configuration - and will
compose through the bubblewrap sandbox automatically when the `lisa-pack-bwrap`
pack is installed.
