# MiniCode Sessio Integration — Spec

## Overview

When MiniCode connects via SSH and detects sessio sessions, show a native picker.
Remember the chosen session per SSH host. On reconnect, auto-attach to the
remembered session — the user goes from "open app" to "inside their session" with
zero interaction.

## Detection

On SSH session open, MiniCode watches terminal output for the marker line:

```
Available sessions:
```

Followed by one or more lines matching the pattern:

```
  <name> (pid <number>)  [optional cwd]  [optional — title]
```

Regex for session lines:
```
^\s+(\S+)\s+\(pid\s+\d+\)
```

Capture group 1 = session name.

Detection window: first 5 seconds after shell starts. If the marker is not seen,
do nothing — the server doesn't have sessio or has no sessions.

If the marker is seen but there are zero session lines (output is
`no active sessions`), do nothing.

## Session Picker UI

When sessions are detected, show a bottom sheet or dialog:

```
┌─────────────────────────────┐
│  Connect to session         │
│                             │
│  ● myproject   /opt/dev/mp  │
│    api-server  /opt/dev/api │
│    docs        /opt/dev/doc │
│                             │
│  [Skip]                     │
└─────────────────────────────┘
```

- List sessions in the order received (server sorts by mtime, most recent first)
- Show CWD if present in the output
- Highlight the remembered session (if any) with ● or bold
- Pre-select the remembered session
- **Skip** dismisses the picker and drops to normal shell

## Connecting to a Session

When the user taps a session name:

1. Send to terminal: `sessio attach <name>\n`
2. Save the choice: `(sshHost, sshPort, sshUser) → sessionName`
3. Dismiss the picker

Storage key: `sessio_session:<user>@<host>:<port>` → `<sessionName>`

Store in `SharedPreferences` or the existing connection settings database.

## Auto-Reconnect

MiniCode already has persistent SSH connections via foreground service. When a
connection drops and re-establishes (or when the user brings the app to foreground
and the session is dead):

### Flow

1. SSH connection re-established → shell starts
2. MiniCode sees `Available sessions:` output (from `.bashrc`)
3. Check if there is a remembered session for this host
4. If yes AND the remembered session name appears in the list:
   - **Skip the picker** — immediately send `sessio attach <name>\n`
   - Show a brief toast: `Reconnected to <name>`
5. If remembered session is NOT in the list (session was killed):
   - Show the picker as normal
   - Clear the remembered session
6. If no remembered session:
   - Show the picker as normal

### Timing

Auto-attach should fire within 500ms of detecting the session list. The user
should perceive it as instant — open app → see their session.

## Clearing the Remembered Session

The remembered session is cleared when:

- The user taps **Skip** in the picker
- The remembered session name is not found in the session list
- The user manually edits the SSH connection settings
- The user long-presses a session in the picker and selects "Forget"

## Data Model

```kotlin
// In ConnectionRepository or SettingsRepository
data class SessioPreference(
    val sessionName: String,
    val lastConnected: Long  // epoch millis, for display
)

// Key: "${user}@${host}:${port}"
fun getSessioSession(hostKey: String): SessioPreference?
fun setSessioSession(hostKey: String, pref: SessioPreference)
fun clearSessioSession(hostKey: String)
```

## Terminal Output Parsing

Add to `TerminalSessionBridge` or a new `SessioDetector` class:

```kotlin
class SessioDetector {
    enum class State { WAITING, COLLECTING, DONE }

    private var state = State.WAITING
    private val sessions = mutableListOf<SessioSession>()
    private var deadline: Long = 0

    data class SessioSession(val name: String, val cwd: String?)

    // Called on every line of terminal output during the detection window
    fun onLine(line: String): List<SessioSession>? {
        if (state == State.DONE) return null
        if (System.currentTimeMillis() > deadline) {
            state = State.DONE
            return null
        }

        when (state) {
            State.WAITING -> {
                if (line.trim() == "Available sessions:") {
                    state = State.COLLECTING
                }
            }
            State.COLLECTING -> {
                val match = SESSION_REGEX.find(line)
                if (match != null) {
                    val name = match.groupValues[1]
                    // CWD is the text between ")  " and end or " — "
                    val rest = line.substringAfter(")").trim()
                    val cwd = rest.substringBefore("—").trim()
                        .ifEmpty { null }
                    sessions.add(SessioSession(name, cwd))
                } else if (sessions.isNotEmpty()) {
                    // Non-matching line after sessions = end of list
                    state = State.DONE
                    return sessions.toList()
                } else if ("no active sessions" in line) {
                    state = State.DONE
                    return null
                }
            }
            else -> {}
        }
        return null
    }

    fun start() {
        state = State.WAITING
        sessions.clear()
        deadline = System.currentTimeMillis() + 5000
    }

    companion object {
        val SESSION_REGEX = Regex("""^\s+(\S+)\s+\(pid\s+\d+\)""")
    }
}
```

## Integration Points

### TerminalSessionBridge

```kotlin
// On SSH session established:
sessioDetector.start()

// In output processing (already called for every line):
val detected = sessioDetector.onLine(line)
if (detected != null) {
    onSessioSessionsDetected(detected)
}
```

### TerminalFragment / Activity

```kotlin
fun onSessioSessionsDetected(sessions: List<SessioSession>) {
    val hostKey = "${sshUser}@${sshHost}:${sshPort}"
    val remembered = repository.getSessioSession(hostKey)

    if (remembered != null && sessions.any { it.name == remembered.sessionName }) {
        // Auto-attach
        sendToTerminal("sessio attach ${remembered.sessionName}\n")
        showToast("Reconnected to ${remembered.sessionName}")
    } else {
        if (remembered != null) {
            repository.clearSessioSession(hostKey)
        }
        showSessioPickerSheet(sessions, hostKey)
    }
}

fun onSessioSessionPicked(session: SessioSession, hostKey: String) {
    repository.setSessioSession(hostKey, SessioPreference(session.name, System.currentTimeMillis()))
    sendToTerminal("sessio attach ${session.name}\n")
}
```

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No sessio on server | No marker detected, no picker shown |
| Sessions exist but none running | `no active sessions` → no picker |
| Remembered session killed | Not in list → show picker, clear memory |
| Multiple SSH connections to same host | Each tab has independent detection, shares remembered session |
| User detaches (Ctrl+]) and returns to shell | Back to normal shell — no re-detection (detection window expired). User types `cs <name>` manually |
| Server prints "Available sessions:" in other context | Unlikely false positive — only checked in first 5s |
| Connection drops mid-session | SSH reconnects → shell restarts → `.bashrc` prints list → auto-attach fires |

## UX Summary

| Action | Result |
|--------|--------|
| First SSH connect | See picker → tap session → attached, session remembered |
| App reopened / reconnect | Auto-attached to remembered session (no interaction) |
| Session was killed | Picker shown, pick new session |
| Skip picker | Normal shell, remembered session cleared |
