# Changelog

All notable changes to the Lisa bash pack are documented here.

## [0.1.1]

### Fixed

#### Subprocess output is decoded with the encoding it carries

- **What:** the pack decoded every subprocess stream as UTF-8. It now reads
  the encoding from the bytes: a UTF-16LE, UTF-16BE, or UTF-8 byte-order
  mark settles it outright, and a stream with no mark is sniffed — UTF-16
  ASCII text has roughly every other byte NUL, so a high NUL density means
  UTF-16 and the parity of the first non-NUL byte settles the endianness.
- **Why:** on Windows the `bash` on PATH is frequently the system's WSL
  launcher, which writes its output as UTF-16. Decoded as UTF-8, the output
  arrived with a NUL between every character, so the command's own text was
  unrecognizable and the pack reported launcher noise in place of the
  result.
- **What:** because the drain is a stream, a chunk boundary can fall in the
  middle of a character. The incremental decoders hold the incomplete tail
  until the next chunk, so a split character no longer corrupts the output.
- **What:** a chunk that is clearly not UTF-16 is released immediately
  rather than held for the sniff window to fill — streaming output has to
  reach the operator surface as it arrives.

#### The pack resolves a bash it can actually drive

- **What:** the pack now resolves the bash it runs. A launcher on PATH is
  passed over in favour of a real POSIX bash from the well-known
  locations — Git for Windows ships one — and the resolved path is what
  the session wrapper and the baseline probe actually invoke.
- **Why:** the Windows WSL launcher accepts no `-c` argument, so the
  pack's composed command never ran. The output was a launcher message
  about missing distributions rather than the command's own output, and
  the exit code was the launcher's rather than the command's.
- **What:** `bash_available` follows the same resolution, so a host with
  only the launcher reports the pack unavailable and the user gets the
  install instruction instead of a confusing launcher error.

### Changed

- The pack now ships a CI workflow that gates it with `ruff format`,
  `ruff check`, `mypy`, and its non-live tests on Linux, macOS, and
  Windows, and `.gitattributes` keeps line endings as LF so a Windows
  checkout cannot rewrite them and fail the format check.

## [0.1.0]

- First public release.
