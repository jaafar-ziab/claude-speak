# Changelog

All notable changes to the `speak` plugin are documented here. This project
follows [Semantic Versioning](https://semver.org/).

## [0.1.1]

### Changed

- **Windows voice upgrade — WinRT (OneCore) with SAPI fallback.**
  `scripts/tts.ps1` now attempts to use the Windows Runtime
  (`Windows.Media.SpeechSynthesis`) speech engine first, which exposes modern
  OneCore voices (e.g. Microsoft George) visible in Windows Settings > Speech
  but invisible to the legacy SAPI/`System.Speech` stack. If WinRT
  initialisation fails, or if a WinRT speak call fails mid-session, the worker
  falls back transparently to SAPI for the remainder of the session.
  Preferred voice changed from **Microsoft Zira Desktop** to
  **Microsoft George Desktop** in both the WinRT and SAPI paths.
- `scripts/tts.py` (the inline SAPI fallback PowerShell command used on
  Windows when the `.ps1` worker is unavailable): updated preferred voice from
  `Microsoft Zira Desktop` to `Microsoft George Desktop`.

## [0.1.0] - 2026-08-11

### Added

- Initial plugin release: `/speak:toggle` skill plus `SessionStart`,
  `UserPromptSubmit`, `PreToolUse`, `MessageDisplay`, and `Stop` hooks wired
  to a bundled `scripts/tts.py`, speaking assistant output aloud via the
  OS's native text-to-speech (`say` on macOS, SAPI on Windows,
  `spd-say`/`espeak` on Linux).
- TTS state (`.tts_enabled`, `.tts_say_pid`, `.tts_queue/`) resolves against
  `CLAUDE_PROJECT_DIR` rather than the plugin's own install path, so it
  stays per-project and correct regardless of where the plugin cache places
  the running script.
- `scripts/tts.ps1`, a native PowerShell implementation used on Windows in
  place of `scripts/tts.py`. Windows frequently shadows `python`/`python3`
  with "App Execution Alias" stub executables that redirect to the
  Microsoft Store and fail to launch — a failure mode that also silently
  defeats a plain shell `||` fallback, since it throws before an exit code
  is ever set. Routing Windows through PowerShell (always present, never
  aliased) removes the dependency on a working system Python entirely.
  macOS/Linux are unaffected and keep using `scripts/tts.py`.
