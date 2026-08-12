#Requires -Version 5.1
<#
Claude Code TTS hook  ->  scripts/tts.ps1
Windows-native counterpart to scripts/tts.py. PowerShell ships with every
Windows install; python/python3/py frequently don't, because Windows
registers "App Execution Alias" stub executables for those names that
redirect to the Microsoft Store and fail to launch outright when nothing
real is linked. That failure mode also isn't a clean nonzero exit code —
launching one of those stubs throws before PowerShell ever sets
$LASTEXITCODE, which defeats a plain `python3 ... || python ...` shell
fallback. Routing Windows through this script instead removes the
dependency on any system Python entirely. macOS/Linux keep using tts.py,
where hooks run through a real POSIX shell and none of this applies.

Behavior mirrors tts.py: same state files, same hook dispatch, same text
cleaning, same background worker/queue design so multiple narrated steps
play out in order instead of cutting each other off. The one structural
difference is the worker's speech playback — since this script already IS
a PowerShell process, the worker just keeps one SpeechSynthesizer alive
in-process for the life of the queue, rather than Python's approach of
spawning and piping to a separate persistent PowerShell process.

The enabled flag, worker PID, and queue are keyed by CLAUDE_CODE_SESSION_ID
(exported to every subprocess Claude Code spawns) rather than shared per
project, so two sessions open on the same project get independent on/off
state and don't fight over one worker's queue.
#>

param(
    [switch]$SessionStart,
    [switch]$Toggle,
    [switch]$Worker
)

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$StateDir = Join-Path $ProjectDir '.claude'
if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }

$SessionId = if ($env:CLAUDE_CODE_SESSION_ID) { $env:CLAUDE_CODE_SESSION_ID } else { 'default' }
$FlagFile = Join-Path $env:TEMP "claude_tts_enabled_$SessionId"
$PidFile  = Join-Path $env:TEMP "claude_tts_pid_$SessionId"
$QueueDir = Join-Path $env:TEMP "claude_tts_queue_$SessionId"
$LogFile  = Join-Path $StateDir 'tts.log'

# ---------- text cleaning ----------

$FileExtensions = @(
    'py','md','txt','json','yaml','yml','toml','ini','cfg','conf',
    'js','jsx','ts','tsx','html','htm','css','scss','sh','ps1',
    'psm1','log','csv','sql','go','rs','java','kt','c','h','cpp',
    'hpp','cc','rb','php','xml','lock','env','svg','png','jpg',
    'jpeg','gif','pdf','zip','gz'
) | Sort-Object -Property Length -Descending -Unique
$FileExtAlt = [string]::Join('|', ($FileExtensions | ForEach-Object { [regex]::Escape($_) }))
$FilePathRegex = [regex]::new(
    "(?<![\w/\\-])((?:\.{0,2}[\w-]+[/\\])*\.{0,2}[\w-]+\.(?:$FileExtAlt))(?![\w/\\.-])",
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)

function Add-FilePathAnnouncements([string]$Text) {
    return $FilePathRegex.Replace($Text, [System.Text.RegularExpressions.MatchEvaluator]{
        param($m)
        $token = $m.Value
        $lead = if ($token -match '[/\\]') { 'in the directory' } else { 'in file' }
        return "$lead $token"
    })
}

function ConvertTo-SpeechText([string]$Text) {
    if (-not $Text) { return '' }
    $t = $Text
    $t = [regex]::Replace($t, '```[\s\S]*?```', 'code block omitted')
    $t = [regex]::Replace($t, '`([^`\n]+)`', '$1')
    $t = [regex]::Replace($t, '(?m)^\s*[\|+][-|:]+[\|+]\s*$', '')
    $t = [regex]::Replace($t, 'https?://\S+', 'URL')
    $t = [regex]::Replace($t, '[#*_~>]', '')
    $t = Add-FilePathAnnouncements $t
    $t = [regex]::Replace($t, '\s+', ' ')
    return $t.Trim()
}

function Get-BlockText($Content) {
    if ($null -eq $Content) { return '' }
    if ($Content -is [string]) { return $Content.Trim() }
    if ($Content -is [array]) {
        $parts = New-Object System.Collections.Generic.List[string]
        foreach ($block in $Content) {
            if ($block -and $block.type -eq 'text') {
                $t = [string]$block.text
                if ($t) { $t = $t.Trim() }
                if ($t) { $parts.Add($t) }
            }
        }
        return [string]::Join(' ', $parts)
    }
    return ''
}

# ---------- detached process launch (survives the hook's Job Object) ----------

if (-not ([System.Management.Automation.PSTypeName]'ClaudeTts.ProcessNative').Type) {
    Add-Type -Namespace ClaudeTts -Name ProcessNative -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
public static extern bool CreateProcess(
    string lpApplicationName, string lpCommandLine,
    IntPtr lpProcessAttributes, IntPtr lpThreadAttributes,
    bool bInheritHandles, uint dwCreationFlags,
    IntPtr lpEnvironment, string lpCurrentDirectory,
    ref STARTUPINFO lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);

[StructLayout(LayoutKind.Sequential)]
public struct STARTUPINFO {
    public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
    public int dwX; public int dwY; public int dwXSize; public int dwYSize;
    public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute;
    public int dwFlags; public short wShowWindow; public short cbReserved2;
    public IntPtr lpReserved2; public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;
}

[StructLayout(LayoutKind.Sequential)]
public struct PROCESS_INFORMATION {
    public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId;
}
'@
}

function Start-DetachedProcess([string]$FilePath, [string]$Arguments) {
    # CREATE_BREAKAWAY_FROM_JOB: Claude Code's own hook process commonly runs
    # inside a Job Object that kills all descendants the moment it exits.
    # Without breakaway, the worker would die right as this hook returns,
    # before it ever gets to speak anything. Some sandboxes disallow
    # breakaway, so fall back to a normal (non-breakaway) launch if it fails.
    $CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    $CREATE_NO_WINDOW = 0x08000000
    $CREATE_NEW_PROCESS_GROUP = 0x00000200
    $flags = $CREATE_BREAKAWAY_FROM_JOB -bor $CREATE_NO_WINDOW -bor $CREATE_NEW_PROCESS_GROUP

    $si = New-Object ClaudeTts.ProcessNative+STARTUPINFO
    $si.cb = [System.Runtime.InteropServices.Marshal]::SizeOf($si)
    $pi = New-Object ClaudeTts.ProcessNative+PROCESS_INFORMATION

    $cmdLine = "`"$FilePath`" $Arguments"
    try {
        $ok = [ClaudeTts.ProcessNative]::CreateProcess($null, $cmdLine, [IntPtr]::Zero, [IntPtr]::Zero, $false, $flags, [IntPtr]::Zero, $null, [ref]$si, [ref]$pi)
    } catch { $ok = $false }
    if (-not $ok) {
        try {
            $proc = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WindowStyle Hidden -PassThru
            return $proc.Id
        } catch { return $null }
    }
    return $pi.dwProcessId
}

# ---------- worker lifecycle ----------

function Test-WorkerAlive {
    if (-not (Test-Path $PidFile)) { return $false }
    try { $procId = [int](Get-Content $PidFile -Raw).Trim() } catch { return $false }
    return $null -ne (Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

function Start-Worker {
    if (-not (Test-Path $QueueDir)) { New-Item -ItemType Directory -Path $QueueDir -Force | Out-Null }
    $psExe = (Get-Process -Id $PID).Path
    $newPid = Start-DetachedProcess -FilePath $psExe -Arguments "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Worker"
    if ($newPid) { Set-Content -Path $PidFile -Value $newPid -NoNewline }
}

function Confirm-Worker {
    # Spawn the worker iff none is alive — guarded by an exclusive-create lock
    # file so two hook invocations racing each other (several MessageDisplay
    # chunks firing back to back) can't both spawn their own worker. Two live
    # workers draining the same queue means two concurrent SpeechSynthesizer
    # instances, which can step on each other's voice selection.
    if (-not (Test-Path $QueueDir)) { New-Item -ItemType Directory -Path $QueueDir -Force | Out-Null }
    if (Test-WorkerAlive) { return }
    $lockPath = "$PidFile.lock"
    try {
        $fs = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
        $fs.Close()
    } catch {
        try {
            if ((Test-Path $lockPath) -and ((Get-Date) - (Get-Item $lockPath).LastWriteTime).TotalSeconds -gt 5) {
                Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
            }
        } catch {}
        return
    }
    try {
        if (-not (Test-WorkerAlive)) { Start-Worker }
    } finally {
        Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Worker {
    if (-not (Test-Path $QueueDir)) { New-Item -ItemType Directory -Path $QueueDir -Force | Out-Null }

    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try { $synth.SelectVoice('Microsoft Zira Desktop') } catch {}
    $synth.Rate = -1

    $idle = 0
    while ($true) {
        $stopMarker = Join-Path $QueueDir '.stop'
        if (Test-Path $stopMarker) {
            Remove-Item $stopMarker -Force -ErrorAction SilentlyContinue
            break
        }
        if (-not (Test-Path $QueueDir)) { break }
        $chunks = @(Get-ChildItem $QueueDir -Filter '*.chunk' -ErrorAction SilentlyContinue | Sort-Object Name)
        if ($chunks.Count -eq 0) {
            $idle++
            if ($idle -ge 4000) { break }   # ~10 minutes at 150ms
            Start-Sleep -Milliseconds 150
            continue
        }
        $idle = 0
        $chunkFile = $chunks[0]
        $text = ''
        try { $text = Get-Content $chunkFile.FullName -Raw -ErrorAction Stop } catch {}
        Remove-Item $chunkFile.FullName -Force -ErrorAction SilentlyContinue
        if ($text) {
            try { $synth.Speak($text) } catch {}
        }
    }

    try { $synth.Dispose() } catch {}

    if (Test-Path $PidFile) {
        try {
            if ((Get-Content $PidFile -Raw).Trim() -eq [string]$PID) {
                Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }
}

function Stop-Say {
    # Hard interrupt: stop whatever's playing/queued. Reserved for a genuinely
    # new user message or TTS being toggled off — not every step within a
    # turn, or steps would keep cutting each other off.
    if (Test-Path $PidFile) {
        try {
            $procId = [int](Get-Content $PidFile -Raw).Trim()
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } catch {}
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $QueueDir) {
        Get-ChildItem $QueueDir -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }
    Remove-Item (Join-Path $env:TEMP "claude_tts_spoken_text_$SessionId") -Force -ErrorAction SilentlyContinue
}

function Add-ToQueue([string]$Text) {
    $cleaned = ConvertTo-SpeechText $Text
    if (-not $cleaned) { return }

    # Skip if this exact text was already queued recently in this session —
    # guards against two hook paths (e.g. MessageDisplay + PreToolUse's or
    # Stop's fallback) both speaking the same content. The check-then-write
    # below runs under a named mutex because it's genuinely racy: two hook
    # processes firing close together for the same turn (most likely right
    # at the start of a session, before MessageDisplay has had time to mark
    # streaming as confirmed) can both read "not yet spoken" before either
    # has written, and both queue the same text — without the lock, this is
    # the mechanism behind hearing the same line spoken twice.
    $spokenTextFile = Join-Path $env:TEMP "claude_tts_spoken_text_$SessionId"
    $mutex = New-Object System.Threading.Mutex($false, "ClaudeTtsDedup_$SessionId")
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne(2000)
        $prior = ''
        if (Test-Path $spokenTextFile) {
            try { $prior = Get-Content $spokenTextFile -Raw -ErrorAction Stop } catch { $prior = '' }
        }
        if ($prior -and $prior.Contains($cleaned)) { return }
        try {
            $updated = $prior + $cleaned + "`n"
            if ($updated.Length -gt 4000) { $updated = $updated.Substring($updated.Length - 4000) }
            Set-Content -Path $spokenTextFile -Value $updated -NoNewline
        } catch {}
    } finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }

    Confirm-Worker
    if (-not (Test-Path $QueueDir)) { New-Item -ItemType Directory -Path $QueueDir -Force | Out-Null }
    $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 6)
    $chunkPath = Join-Path $QueueDir "$stamp`_$suffix.chunk"
    Set-Content -Path $chunkPath -Value $cleaned -NoNewline -Encoding UTF8
}

# ---------- session lifecycle ----------

function Reset-SessionState {
    # TTS is off by default for every new session, regardless of how a
    # previous one left it. Also tears down any leftover worker/queue from a
    # session that ended uncleanly.
    Remove-Item $FlagFile -Force -ErrorAction SilentlyContinue
    if (Test-Path $PidFile) {
        try {
            $procId = [int](Get-Content $PidFile -Raw).Trim()
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } catch {}
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $QueueDir -Recurse -Force -ErrorAction SilentlyContinue
}

function Invoke-Toggle {
    if (Test-Path $FlagFile) {
        Remove-Item $FlagFile -Force -ErrorAction SilentlyContinue
        Stop-Say
        Write-Output 'TTS off'
    } else {
        [DateTime]::UtcNow.ToString('o') | Set-Content -Path $FlagFile -NoNewline
        Write-Output 'TTS on - will now speak messages from this point forward'
    }
}

# ---------- argv dispatch ----------

if ($Worker) { Invoke-Worker; exit 0 }
if ($SessionStart) { Reset-SessionState; exit 0 }
if ($Toggle) { Invoke-Toggle; exit 0 }

# UserPromptSubmit fires before the flag-file check below, and before Claude
# even sees the prompt, because /speak:toggle needs to work in both
# directions (including off -> on) and needs to be caught here, before it
# would otherwise expand into the (now-vestigial) toggle skill.
try {
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { exit 0 }
    $data = $raw | ConvertFrom-Json
} catch { exit 0 }
if (-not $data) { exit 0 }

try {
    (($data | ConvertTo-Json -Compress -Depth 10) + "`n---") | Add-Content -Path $LogFile -Encoding UTF8
} catch {}

$HookEvent = [string]$data.hook_event_name

if ($HookEvent -eq 'UserPromptSubmit') {
    $prompt = [string]$data.prompt
    if ($prompt -and $prompt.Trim() -eq '/speak:toggle') {
        # Handle the toggle entirely here — via decision:block — so Claude
        # never sees this as a request to run anything, and the skill's own
        # fallback invocation (kept for the rare case interception doesn't
        # fire) never executes and never shows an approval prompt.
        if (Test-Path $FlagFile) {
            Remove-Item $FlagFile -Force -ErrorAction SilentlyContinue
            Stop-Say
            $reason = 'TTS off'
        } else {
            [DateTime]::UtcNow.ToString('o') | Set-Content -Path $FlagFile -NoNewline
            $reason = 'TTS on - will now speak messages from this point forward'
        }
        Write-Output ([pscustomobject]@{ decision = 'block'; reason = $reason } | ConvertTo-Json -Compress)
        exit 0
    }
    # A genuinely new user message — interrupt whatever's still playing/queued
    # from the previous turn.
    if (Test-Path $FlagFile) { Stop-Say }
    exit 0
}

if (-not (Test-Path $FlagFile)) { exit 0 }

$StreamingFlag = Join-Path $env:TEMP "claude_tts_streaming_$SessionId"
$PrevDeltaFile = Join-Path $env:TEMP "claude_tts_delta_$SessionId"

if ($HookEvent -eq 'MessageDisplay') {
    $raw2 = $null
    foreach ($key in @('delta', 'message', 'text', 'content')) {
        $v = $data.$key
        if ($v) { $raw2 = $v; break }
    }
    if ($null -eq $raw2) {
        $lines = $data.lines
        $raw2 = if ($lines -is [array]) { [string]::Join("`n", $lines) } else { $lines }
    }
    $chunk = if ($raw2 -is [array]) { Get-BlockText $raw2 } else { ([string]$raw2).Trim() }

    if ($chunk) {
        $prev = ''
        if (Test-Path $PrevDeltaFile) {
            try { $prev = Get-Content $PrevDeltaFile -Raw -ErrorAction Stop } catch { $prev = '' }
        }
        $newPart = if ($prev -and $chunk.StartsWith($prev)) { $chunk.Substring($prev.Length) } else { $chunk }
        try { Set-Content -Path $PrevDeltaFile -Value $chunk -NoNewline -Encoding UTF8 } catch {}
        if ($newPart.Trim()) {
            Add-ToQueue $newPart
            try { Set-Content -Path $StreamingFlag -Value '1' -NoNewline } catch {}
        }
    }
    exit 0
}

$IsStop = ($HookEvent -eq 'Stop') -or ($null -ne $data.last_assistant_message)

if ($IsStop -and (Test-Path $StreamingFlag)) {
    Remove-Item $PrevDeltaFile -Force -ErrorAction SilentlyContinue
    exit 0
}

if ($IsStop) {
    $raw3 = $data.last_assistant_message
    $text = if ($raw3 -is [array]) { Get-BlockText $raw3 } else { ([string]$raw3).Trim() }
    Add-ToQueue $text

    $stopTranscriptPath = [string]$data.transcript_path
    if ($stopTranscriptPath -and (Test-Path $stopTranscriptPath)) {
        try {
            $lastUuid = ''
            foreach ($line in Get-Content $stopTranscriptPath -ErrorAction Stop) {
                try { $entry = $line | ConvertFrom-Json } catch { continue }
                if ($entry.type -eq 'assistant') { $lastUuid = [string]$entry.uuid }
            }
            if ($lastUuid) {
                Add-Content -Path (Join-Path $env:TEMP "claude_tts_$SessionId") -Value $lastUuid
            }
        } catch {}
    }
    exit 0
}

if (Test-Path $StreamingFlag) { exit 0 }

$TranscriptPath = [string]$data.transcript_path
$ToolUseId = [string]$data.tool_use_id
if (-not $TranscriptPath -or -not (Test-Path $TranscriptPath) -or -not $ToolUseId) { exit 0 }

$SpokenFile = Join-Path $env:TEMP "claude_tts_$SessionId"
$SpokenUids = New-Object System.Collections.Generic.HashSet[string]
if (Test-Path $SpokenFile) {
    foreach ($line in (Get-Content $SpokenFile -ErrorAction SilentlyContinue)) {
        if ($line) { [void]$SpokenUids.Add($line) }
    }
}

$Entries = New-Object System.Collections.Generic.List[object]
foreach ($line in Get-Content $TranscriptPath -ErrorAction SilentlyContinue) {
    try { $Entries.Add(($line | ConvertFrom-Json)) } catch { continue }
}

# Find the tool-use entry matching our tool_use_id, and where in its content
# array that specific tool_use block sits.
$ToolUseIdx = -1
$BlockIdxInEntry = -1
for ($i = 0; $i -lt $Entries.Count; $i++) {
    $entry = $Entries[$i]
    if ($entry.type -ne 'assistant') { continue }
    $content = $entry.message.content
    if ($null -eq $content -or -not ($content -is [array])) { continue }
    for ($bi = 0; $bi -lt $content.Count; $bi++) {
        $block = $content[$bi]
        if ($block -and $block.type -eq 'tool_use' -and $block.id -eq $ToolUseId) {
            $ToolUseIdx = $i
            $BlockIdxInEntry = $bi
            break
        }
    }
    if ($ToolUseIdx -ge 0) { break }
}

if ($ToolUseIdx -eq -1) { exit 0 }

$NewTexts = New-Object System.Collections.Generic.List[string]
$NewUids = New-Object System.Collections.Generic.List[string]

# Commentary text often lives in the SAME entry as the tool call it precedes,
# so it never shows up as its own entry. Grab the text between the previous
# tool_use block (or the start of the entry) and this one.
$Content = $Entries[$ToolUseIdx].message.content
$PrevToolIdx = -1
for ($j = $BlockIdxInEntry - 1; $j -ge 0; $j--) {
    $b = $Content[$j]
    if ($b -and $b.type -eq 'tool_use') { $PrevToolIdx = $j; break }
}
$LeadingSlice = @(if (($PrevToolIdx + 1) -lt $BlockIdxInEntry) { $Content[($PrevToolIdx + 1)..($BlockIdxInEntry - 1)] } else { @() })
$LeadingText = Get-BlockText $LeadingSlice
if ($LeadingText) { $NewTexts.Add($LeadingText) }

# Walk backward from the tool-use entry to collect unspoken standalone text
# entries (text-only assistant turns that precede a later, separate tool call).
for ($i = $ToolUseIdx - 1; $i -ge 0; $i--) {
    $entry = $Entries[$i]
    if ($entry.type -ne 'assistant') { continue }
    $uid = [string]$entry.uuid
    if ($SpokenUids.Contains($uid)) { break }
    if ($entry.message.stop_reason -eq 'tool_use') { continue }
    $text = Get-BlockText $entry.message.content
    if ($text) {
        $NewTexts.Insert(0, $text)
        $NewUids.Insert(0, $uid)
    }
}

if ($NewUids.Count -gt 0) {
    Add-Content -Path $SpokenFile -Value ([string]::Join("`n", $NewUids))
}

# Each piece is queued separately (rather than joined into one block) so the
# worker's pacing matches the actual step boundaries.
foreach ($t in $NewTexts) { Add-ToQueue $t }
