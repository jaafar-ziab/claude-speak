# claude-speak

A Claude Code plugin that gives Claude a voice. While Claude works, it speaks
its running commentary aloud using your operating system's built-in
text-to-speech — no API keys, no cloud service, nothing to sign up for.

Useful any time you're not staring at the screen: let Claude narrate what
it's doing — reading files, running commands, explaining a fix — while you
look away, and know the moment it's actually asking for your input.

## Features

- **Zero-config speech** — `say` on macOS, SAPI on Windows, `spd-say`/`espeak`
  on Linux. Whatever your OS already ships with.
- **Streams as Claude types** — speaks each chunk of a response as it's
  written, not after the fact.
- **Off by default** — every new session starts silent; toggle it on with one
  command.
- **Reads file paths out loud sensibly** — announces `tts.py` as "in file
  tts.py" and `src/hooks/` as "in the directory src/hooks" instead of reading
  punctuation.

## Install

From inside a Claude Code session:

```
/plugin marketplace add jaafar-ziab/claude-speak
/plugin install speak@speak-tts
```

By default this installs for you across every project (`user` scope). To
scope it to just the current project instead, add `--scope project` to the
install command.

## Requirements

- **Windows** — nothing to install. Runs on PowerShell, which ships with
  every Windows machine, and speaks through SAPI, which ships with Windows
  too.
- **macOS** — nothing to install. Speaks through `say`, and the hook itself
  needs Python 3 (`python3` on your `PATH`), which macOS ships by default.
- **Linux** — needs Python 3 (`python3` on your `PATH`, standard on most
  distributions) plus a TTS engine installed separately, e.g. `sudo apt
  install speech-dispatcher` (for `spd-say`) or `sudo apt install espeak`.
  Without one, the plugin still runs, it just won't produce sound.

## Usage

```
/speak:toggle
```

Toggles speech on/off for the current session. It always starts off at the
beginning of a new session, regardless of how you left it last time.

## Shaping how Claude sounds

The plugin only *speaks whatever Claude already writes* — it doesn't change
what Claude says. If you also want Claude to phrase things in a more
speech-friendly way (a short spoken lead-in before it acts, file paths
narrated instead of silently touched), add instructions like that to your
own `CLAUDE.md` — project-level or `~/.claude/CLAUDE.md` for every project.
That's a separate, optional step; this plugin only provides the speaking
mechanism itself.

## Uninstall

```
/plugin uninstall speak@speak-tts
```

## License

MIT — see [LICENSE](LICENSE).
