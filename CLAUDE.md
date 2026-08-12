# Voice / TTS output

TTS is active in this project (`.claude/scripts/tts.py` speaks assistant text aloud). The user may not be looking at the screen, so shape responses to still make sense heard, not just read:

- Before starting non-trivial work, say one short sentence summarizing what you're about to do, before diving in.
- When you're about to read or work in a specific file or directory, say the path out loud in plain words (e.g. "checking the tts script in dot-claude scripts") in addition to any code span. The TTS hook now speaks backtick-wrapped text too (it strips the backticks but keeps the content), and for plain-prose path mentions it prefixes bare filenames with "in file" and multi-segment paths with "in the directory" before reading them.
- When you need the user to make a choice (e.g. via AskUserQuestion), say so explicitly out loud — e.g. "Choose the option you need so I can continue" — since the options themselves render on screen, not in speech.
- Before calling AskUserQuestion, and before every single PowerShell/Bash command (no exceptions — don't try to guess whether it's already allowlisted), output a spoken sentence first, ending with "Please choose the suitable option for you to continue further." Say this before each individual command, even several in a row in the same turn — both the AskUserQuestion UI and the permission-approval dialog are visual-only and speak nothing on their own.
