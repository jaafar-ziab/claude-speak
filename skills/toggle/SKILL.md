---
name: toggle
description: Toggle text-to-speech output on or off for this session.
---

Toggle text-to-speech on or off.

Normally you'll never actually run this: the `UserPromptSubmit` hook
recognizes `/speak:toggle` as plain prompt text and handles the toggle
itself via `decision: block`, before this skill's instructions are ever
reached. This is only reached if interception didn't fire (e.g. an older
Claude Code build without `UserPromptSubmit` support).

On Windows, run the following command and report its output to the user:

```bash
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/tts.ps1" -Toggle
```

On macOS/Linux there is no separate fallback — `tts.py` has no CLI toggle
path of its own, since the hook is the only mechanism. Tell the user the
toggle hook doesn't appear to be firing and suggest restarting the session.
