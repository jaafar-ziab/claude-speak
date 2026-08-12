#!/usr/bin/env python3
"""
Claude Code TTS hook  ->  scripts/tts.py
Wired to the UserPromptSubmit, MessageDisplay, PreToolUse and Stop hooks.
Speaks assistant text via the OS's native TTS: `say` on macOS, `espeak`/
`spd-say` on Linux. Platform is auto-detected at runtime.

This is the primary script for macOS/Linux. On Windows, scripts/tts.ps1
handles everything natively and this script is only reached if PowerShell
itself is somehow unavailable — an edge case kept for defense in depth,
not something expected to fire in practice. Its Windows branch still
speaks via SAPI (System.Speech), through one persistent PowerShell process
kept alive for the life of the worker (see `_get_speech_process`), fed one
line per chunk over stdin, rather than a fresh `powershell.exe` + assembly
load per chunk — that per-chunk spawn cost was audible dead air between
steps.

MessageDisplay fires incrementally while a response is still streaming, so
that's the primary path — it speaks each new batch of text as soon as it's
rendered instead of waiting for the whole message. PreToolUse/Stop's
after-the-fact speech is kept as a fallback for whenever MessageDisplay
doesn't fire (older Claude Code build, unrecognized payload shape); once it's
confirmed working in a session, those two stop duplicating its output.

Text is not spoken directly — it's dropped into a queue, and a persistent
background worker plays queued chunks one at a time, in order, blocking
until each finishes. This lets each narrated step ("I'll start searching",
then later "now let's check the tests") play out fully instead of the next
step's speech cutting off the previous one. The queue is only cleared on a
genuinely new user message (UserPromptSubmit), or when TTS is toggled off.

This script runs from the plugin cache (~/.claude/plugins/cache/...), a path
that has nothing to do with the project and changes on every plugin update —
so state files are resolved against CLAUDE_PROJECT_DIR (which Claude Code
exports to every hook subprocess) instead of this file's own location,
keeping state per-project regardless of where the cache places the script.

The enabled flag, worker PID, and queue are all keyed by CLAUDE_CODE_SESSION_ID
(also exported to every subprocess Claude Code spawns) rather than shared per
project, so two sessions open on the same project get fully independent
on/off state and don't fight over one worker's queue — toggling off in one
session can't cut off speech that belongs to another.

/speak:toggle is handled entirely inside the UserPromptSubmit branch below —
it recognizes the literal prompt text and responds with a decision:block
JSON, so Claude never sees it as a request and never runs a command for it.
There is deliberately no CLI toggle flag on this script; the skill's own
fallback path (for the rare case interception doesn't fire) has no
macOS/Linux equivalent, since that would just reintroduce the thing this
design avoids — a shell command visible in an approval prompt for something
as simple as flipping a flag.
"""
import json, sys, os, subprocess, re, signal, platform, tempfile, shutil, time
from datetime import datetime, timezone

IS_WINDOWS = platform.system() == 'Windows'
IS_MAC = platform.system() == 'Darwin'

project_dir = os.environ.get('CLAUDE_PROJECT_DIR') or os.getcwd()
state_dir = os.path.join(project_dir, '.claude')
os.makedirs(state_dir, exist_ok=True)

session_id = os.environ.get('CLAUDE_CODE_SESSION_ID') or 'default'
flag_file = os.path.join(tempfile.gettempdir(), f'claude_tts_enabled_{session_id}')
pid_file = os.path.join(tempfile.gettempdir(), f'claude_tts_pid_{session_id}')
queue_dir = os.path.join(tempfile.gettempdir(), f'claude_tts_queue_{session_id}')


def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                t = block.get('text', "").strip()
                if t:
                    parts.append(t)
        return " ".join(parts)
    return ""


_FILE_EXTENSIONS = (
    'py', 'md', 'txt', 'json', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
    'js', 'jsx', 'ts', 'tsx', 'html', 'htm', 'css', 'scss', 'sh', 'ps1',
    'psm1', 'log', 'csv', 'sql', 'go', 'rs', 'java', 'kt', 'c', 'h', 'cpp',
    'hpp', 'cc', 'rb', 'php', 'xml', 'lock', 'env', 'svg', 'png', 'jpg',
    'jpeg', 'gif', 'pdf', 'zip', 'gz',
)
# Longest-first so e.g. 'yaml' matches before a hypothetical shorter prefix.
_FILE_EXT_ALT = '|'.join(re.escape(ext) for ext in sorted(set(_FILE_EXTENSIONS), key=len, reverse=True))

# Only real file extensions count as a file mention — this deliberately does
# NOT match arbitrary `word.word` tokens like `provider.complete(request)`,
# which is a method call, not a file.
_FILE_PATH_RE = re.compile(
    r'(?<![\w/\\-])'
    r'((?:\.{0,2}[\w-]+[/\\])*\.{0,2}[\w-]+\.(?:' + _FILE_EXT_ALT + r'))'
    r'(?![\w/\\.-])',
    re.IGNORECASE,
)


def _announce_file_path(match):
    token = match.group(0)
    lead = 'in the directory' if ('/' in token or '\\' in token) else 'in file'
    return f'{lead} {token}'


def announce_file_paths(text):
    """Prefix bare filenames ('base.py') with 'in file' and multi-segment
    paths ('claude/command/tts.py') with 'in the directory' so a listener
    can tell, by ear, whether they're hearing a filename or a path."""
    return _FILE_PATH_RE.sub(_announce_file_path, text)


def clean_for_speech(text):
    text = re.sub(r'```[\s\S]*?```', 'code block omitted', text)   # fenced code
    text = re.sub(r'`([^`\n]+)`', r'\1', text)                     # inline code: keep text, drop backticks
    text = re.sub(r'^\s*[\|+][-|:]+[\|+]\s*$', "", text, flags=re.MULTILINE)  # table rules
    text = re.sub(r'https?://\S+', 'URL', text)                    # links
    text = re.sub(r'[#*_~>]', "", text)                            # markdown chars
    text = announce_file_paths(text)                               # narrate file/dir mentions
    text = re.sub(r'\s+', ' ', text)                               # collapse whitespace
    return text.strip()


def _popen_detached(args):
    """Launch a fully detached background process, cross-platform.

    On Windows, Claude Code's own hook process is typically run inside a Job
    Object that kills all descendants the moment the hook exits. Without
    CREATE_BREAKAWAY_FROM_JOB, the spawned worker gets killed before it can
    do anything, right as the hook script returns. Some sandboxes disallow
    breakaway, so fall back to spawning without it if that's the case.
    """
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if IS_WINDOWS:
        base_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.Popen(
                args, creationflags=base_flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                **kwargs
            )
        except OSError:
            return subprocess.Popen(args, creationflags=base_flags, **kwargs)
    else:
        return subprocess.Popen(args, start_new_session=True, **kwargs)


_speech_proc = None  # persistent PowerShell/SAPI child (Windows only), reused across chunks


def _start_speech_process():
    ps_script = (
        '[Console]::InputEncoding = [System.Text.Encoding]::UTF8; '
        '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
        'Add-Type -AssemblyName System.Speech; '
        '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
        'try { $s.SelectVoice("Microsoft Zira Desktop") } catch {}; '
        '$s.Rate = -1; '
        'while ($true) { '
        '  $line = [Console]::In.ReadLine(); '
        '  if ($line -eq $null -or $line -eq "__EXIT__") { break }; '
        '  $s.Speak($line); '
        '  [Console]::Out.WriteLine("__DONE__"); '
        '  [Console]::Out.Flush(); '
        '}'
    )
    return subprocess.Popen(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', bufsize=1,
    )


def _get_speech_process():
    """Lazily start the SAPI worker once and keep reusing it — spawning
    PowerShell + loading System.Speech per chunk (the old approach) costs
    several hundred ms of dead air between every narrated step."""
    global _speech_proc
    if _speech_proc is not None and _speech_proc.poll() is None:
        return _speech_proc
    _speech_proc = _start_speech_process()
    return _speech_proc


def _stop_speech_process():
    global _speech_proc
    if _speech_proc is None:
        return
    try:
        if _speech_proc.poll() is None:
            try:
                _speech_proc.stdin.write('__EXIT__\n')
                _speech_proc.stdin.flush()
            except Exception:
                pass
            _speech_proc.terminate()
    except Exception:
        pass
    _speech_proc = None


def play_blocking(text):
    """Speak one chunk synchronously — the worker's queue loop waits on this."""
    if IS_MAC:
        subprocess.run(['say', text])
    elif IS_WINDOWS:
        # One retry: if the persistent process died between chunks (crash,
        # killed voice, pipe error), restart it once and try again before
        # giving up on this chunk.
        for attempt in range(2):
            proc = _get_speech_process()
            try:
                proc.stdin.write(text + '\n')
                proc.stdin.flush()
                if not proc.stdout.readline():
                    raise BrokenPipeError("speech process closed stdout")
                break
            except Exception:
                _stop_speech_process()
                if attempt == 1:
                    pass  # drop this chunk rather than loop forever
    else:
        if shutil.which('spd-say'):
            subprocess.run(['spd-say', '-w', text])
        elif shutil.which('espeak'):
            subprocess.run(['espeak', text])


def run_worker():
    """Long-lived queue consumer. One chunk file per queued utterance;
    played strictly in filename (== chronological) order. Exits on a
    '.stop' marker (hard interrupt) or after ~10 minutes of no new chunks.
    """
    os.makedirs(queue_dir, exist_ok=True)
    idle = 0
    while True:
        stop_marker = os.path.join(queue_dir, '.stop')
        if os.path.exists(stop_marker):
            try:
                os.remove(stop_marker)
            except Exception:
                pass
            break
        try:
            chunks = sorted(f for f in os.listdir(queue_dir) if f.endswith('.chunk'))
        except FileNotFoundError:
            break
        if not chunks:
            idle += 1
            if idle >= 4000:  # ~10 minutes at 150ms
                break
            time.sleep(0.15)
            continue
        idle = 0
        path = os.path.join(queue_dir, chunks[0])
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except Exception:
            text = ""
        try:
            os.remove(path)
        except Exception:
            pass
        if text:
            play_blocking(text)

    _stop_speech_process()

    try:
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(pid_file)
    except Exception:
        pass


if len(sys.argv) > 1 and sys.argv[1] == '--worker':
    run_worker()
    sys.exit(0)


def reset_session_state():
    """SessionStart hook: TTS is off by default for every new Claude Code
    session, regardless of whether a previous session left it on. Also tears
    down any leftover worker/queue from a session that ended uncleanly."""
    try:
        if os.path.exists(flag_file):
            os.remove(flag_file)
    except Exception:
        pass
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            if IS_WINDOWS:
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass
    shutil.rmtree(queue_dir, ignore_errors=True)


if len(sys.argv) > 1 and sys.argv[1] == '--session-start':
    reset_session_state()
    sys.exit(0)


def is_worker_alive():
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                  capture_output=True, text=True, timeout=3)
            return str(pid) in out.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def spawn_worker():
    os.makedirs(queue_dir, exist_ok=True)
    proc = _popen_detached([sys.executable, os.path.abspath(__file__), '--worker'])
    with open(pid_file, 'w') as f:
        f.write(str(proc.pid))


def _try_acquire_lock(lock_path, stale_after=5.0):
    """Exclusive-create lock file, atomic and cross-platform via os.open's
    O_CREAT|O_EXCL. Returns True iff acquired. Clears a lock older than
    stale_after — a prior holder that died mid-operation (crash, kill)
    rather than releasing it — so a wedged lock doesn't block forever."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock_path) > stale_after:
                os.remove(lock_path)
        except Exception:
            pass
        return False


def _release_lock(lock_path):
    try:
        os.remove(lock_path)
    except Exception:
        pass


def ensure_worker():
    """Spawn the worker iff none is alive — guarded by a lock so two hook
    invocations racing each other (e.g. several MessageDisplay chunks firing
    back to back) can't both pass the is_worker_alive() check and each spawn
    their own worker. Two live workers draining the same queue means two
    concurrent SpeechSynthesizer processes, and when they collide on
    SelectVoice one can silently fall back to the OS default voice —
    alternating sentences in two different-sounding voices.
    """
    os.makedirs(queue_dir, exist_ok=True)
    if is_worker_alive():
        return
    lock_path = pid_file + '.lock'
    if not _try_acquire_lock(lock_path):
        return
    try:
        if not is_worker_alive():
            spawn_worker()
    finally:
        _release_lock(lock_path)


def enqueue(text):
    cleaned = clean_for_speech(text)
    # Strip lone surrogate code points (e.g. from mangled emoji) that can't
    # be encoded to UTF-8, which would otherwise crash the hook silently.
    cleaned = cleaned.encode('utf-8', 'ignore').decode('utf-8')
    if not cleaned:
        return

    # Guard against two hook paths racing each other and both speaking the
    # same content (e.g. PreToolUse's after-the-fact fallback firing for text
    # that MessageDisplay is also streaming live, before the streaming flag
    # that's supposed to silence the fallback has been written). Skip if this
    # exact text was already queued recently in this session; a rolling
    # buffer, not a session-lifetime set, so a legitimately repeated phrase
    # later in the same turn still speaks.
    #
    # The check-then-write below is locked because it's genuinely racy: two
    # hook processes firing close together for the same turn (most likely
    # right at the start of a session, before MessageDisplay has had time to
    # mark streaming as confirmed) can both read "not yet spoken" before
    # either has written, and both queue the same text — this is the
    # mechanism behind hearing the same line spoken twice.
    spoken_text_file = os.path.join(tempfile.gettempdir(), f'claude_tts_spoken_text_{session_id}')
    lock_path = spoken_text_file + '.lock'
    deadline = time.time() + 2.0
    acquired = False
    while time.time() < deadline:
        if _try_acquire_lock(lock_path):
            acquired = True
            break
        time.sleep(0.02)
    try:
        prior = ""
        if os.path.exists(spoken_text_file):
            try:
                with open(spoken_text_file, encoding='utf-8') as f:
                    prior = f.read()
            except Exception:
                prior = ""
        if cleaned in prior:
            return
        try:
            updated = (prior + cleaned + '\n')[-4000:]
            with open(spoken_text_file, 'w', encoding='utf-8') as f:
                f.write(updated)
        except Exception:
            pass
    finally:
        if acquired:
            _release_lock(lock_path)

    ensure_worker()
    path = os.path.join(queue_dir, f'{time.time_ns()}.chunk')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(cleaned)


def kill_say():
    """Hard interrupt: stop whatever's playing/queued. Reserved for a
    genuinely new user message or TTS being toggled off — NOT for every
    step within a turn, or steps would keep cutting each other off."""
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            if IS_WINDOWS:
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass
    try:
        for fn in os.listdir(queue_dir):
            try:
                os.remove(os.path.join(queue_dir, fn))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    # New turn: old dedup history shouldn't suppress legitimately repeated
    # phrasing going forward.
    try:
        os.remove(os.path.join(tempfile.gettempdir(), f'claude_tts_spoken_text_{session_id}'))
    except Exception:
        pass


# UserPromptSubmit fires before the flag-file check below, and before Claude
# even sees the prompt, because /speak:toggle needs to work in both
# directions (including off -> on) and needs to be caught here, before it
# would otherwise expand into the (now-vestigial) toggle skill.
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

try:
    with open(os.path.join(state_dir, 'tts.log'), 'a', encoding='utf-8') as _f:
        _f.write(json.dumps(data) + '\n---\n')
except Exception:
    pass

hook_event = data.get('hook_event_name', "")

if hook_event == 'UserPromptSubmit':
    prompt = str(data.get('prompt') or "")
    if prompt.strip() == '/speak:toggle':
        # Handle the toggle entirely here — via decision:block — so Claude
        # never sees this as a request to run anything, and the skill's own
        # instructions never execute and never show an approval prompt.
        if os.path.exists(flag_file):
            try:
                os.remove(flag_file)
            except Exception:
                pass
            kill_say()
            reason = "TTS off"
        else:
            with open(flag_file, 'w', encoding='utf-8') as f:
                f.write(datetime.now(timezone.utc).isoformat())
            reason = "TTS on - will now speak messages from this point forward"
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(0)

    # A genuinely new user message — interrupt whatever's still playing/queued
    # from the previous turn.
    if os.path.exists(flag_file):
        kill_say()
    sys.exit(0)

if not os.path.exists(flag_file):
    sys.exit(0)

# Set once MessageDisplay has been confirmed to deliver real text in this
# session, so we know live streaming speech is working. Stop and PreToolUse
# fall back to their old (full-message-after-the-fact) behavior whenever
# this hasn't fired — e.g. an older Claude Code build without the event, or
# the payload not matching any of the field names we try below.
streaming_flag = os.path.join(tempfile.gettempdir(), f'claude_tts_streaming_{session_id}')
# Tracks the last delta text seen, in case the payload turns out to send
# the whole message-so-far each time rather than just the new increment —
# then we only speak the newly-appended suffix instead of repeating it all.
prev_delta_file = os.path.join(tempfile.gettempdir(), f'claude_tts_delta_{session_id}')

if hook_event == 'MessageDisplay':
    raw = None
    for key in ('delta', 'message', 'text', 'content'):
        v = data.get(key, "")
        if v:
            raw = v
            break
    if raw is None:
        lines = data.get('lines', "")
        raw = "\n".join(lines) if isinstance(lines, list) else lines
    chunk = extract_text(raw) if isinstance(raw, list) else str(raw or "").strip()

    if chunk:
        prev = ""
        if os.path.exists(prev_delta_file):
            try:
                with open(prev_delta_file, encoding='utf-8') as f:
                    prev = f.read()
            except Exception:
                prev = ""
        new_part = chunk[len(prev):] if prev and chunk.startswith(prev) else chunk
        try:
            with open(prev_delta_file, 'w', encoding='utf-8') as f:
                f.write(chunk)
        except Exception:
            pass
        if new_part.strip():
            enqueue(new_part)
            try:
                with open(streaming_flag, 'w') as f:
                    f.write('1')
            except Exception:
                pass
    sys.exit(0)

is_stop = hook_event == 'Stop' or 'last_assistant_message' in data

# If MessageDisplay already streamed this turn's text as it was generated,
# skip the full-message replay below — it would just repeat everything.
if is_stop and os.path.exists(streaming_flag):
    try:
        os.remove(prev_delta_file)
    except Exception:
        pass
    sys.exit(0)

# Stop hook: read the message directly from stdin, no jsonl race condition
if is_stop:
    raw = data.get('last_assistant_message', "")
    text = extract_text(raw) if isinstance(raw, list) else str(raw).strip()
    enqueue(text)

    # Mark the corresponding transcript entry as already spoken, otherwise a
    # later PreToolUse walk-back (once some future turn uses a tool) finds
    # this plain-text turn "unspoken" and replays it from the beginning.
    stop_transcript_path = data.get('transcript_path', "")
    if stop_transcript_path and os.path.exists(stop_transcript_path):
        try:
            last_uuid = ""
            with open(stop_transcript_path, encoding='utf-8') as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get('type') == 'assistant':
                        last_uuid = entry.get('uuid', "")
            if last_uuid:
                spoken_file = os.path.join(tempfile.gettempdir(), f'claude_tts_{session_id}')
                with open(spoken_file, 'a') as f:
                    f.write(last_uuid + '\n')
        except Exception:
            pass

    sys.exit(0)

# PreToolUse hook: fires before the tool runs. Text gets queued, not spoken
# immediately — the worker plays it in order without cutting off whatever
# step came before it.
#
# Skipped entirely once MessageDisplay is confirmed working, since it
# already speaks this same leading commentary live as it streams in.
if os.path.exists(streaming_flag):
    sys.exit(0)

transcript_path = data.get('transcript_path', "")
tool_use_id = data.get('tool_use_id', "")
if not transcript_path or not os.path.exists(transcript_path) or not tool_use_id:
    sys.exit(0)

spoken_file = os.path.join(tempfile.gettempdir(), f'claude_tts_{session_id}')
if os.path.exists(spoken_file):
    with open(spoken_file) as f:
        spoken_uids = set(f.read().strip().split('\n'))
else:
    spoken_uids = set()

# Load all jsonl entries
entries = []
with open(transcript_path, encoding='utf-8') as f:
    for line in f:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

# Find the tool-use entry matching our tool_use_id, and where in its
# content array that specific tool_use block sits.
tool_use_idx = None
block_idx_in_entry = None
for i, entry in enumerate(entries):
    if entry.get('type') != 'assistant':
        continue
    content = entry.get('message', {}).get('content', [])
    for bi, block in enumerate(content):
        if isinstance(block, dict) and block.get('type') == 'tool_use' \
                and block.get('id') == tool_use_id:
            tool_use_idx = i
            block_idx_in_entry = bi
            break
    if tool_use_idx is not None:
        break

if tool_use_idx is None:
    sys.exit(0)

new_texts = []
new_uids = []

# Commentary text often lives in the SAME entry as the tool call it precedes
# (e.g. "Let me check the file." followed immediately by a Read block), so it
# never shows up as its own entry. Grab the text between the previous
# tool_use block (or the start of the entry) and this one.
content = entries[tool_use_idx].get('message', {}).get('content', [])
prev_tool_idx = -1
for j in range(block_idx_in_entry - 1, -1, -1):
    b = content[j]
    if isinstance(b, dict) and b.get('type') == 'tool_use':
        prev_tool_idx = j
        break
leading_text = extract_text(content[prev_tool_idx + 1:block_idx_in_entry])
if leading_text:
    new_texts.append(leading_text)

# Walk backward from the tool-use entry to collect unspoken standalone text
# entries (text-only assistant turns that precede a later, separate tool call).
for entry in reversed(entries[:tool_use_idx]):
    if entry.get('type') != 'assistant':
        continue
    uid = entry.get('uuid', "")
    if uid in spoken_uids:
        break
    if entry.get('message', {}).get('stop_reason') == 'tool_use':
        continue
    text = extract_text(entry.get('message', {}).get('content', []))
    if text:
        new_texts.insert(0, text)
        new_uids.insert(0, uid)

if new_uids:
    with open(spoken_file, 'a') as f:
        f.write('\n'.join(new_uids) + '\n')

# Each piece is queued separately (rather than joined into one block) so the
# worker's pacing matches the actual step boundaries.
for t in new_texts:
    enqueue(t)
