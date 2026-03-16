# Sessio — Internal Design Documentation

## Terminal Title Handling (OSC Sequences)

### Setting the title

Sessio sets the terminal tab/window title to the session name using OSC 0:

```
\x1b]0;<session-name>\x07
```

This is emitted:
- By the **client** on attach (`_set_terminal_title` in `RawClient.run` / `LineClient.run`)
- By the **daemon** on every PTY read — appended to output sent to clients

### Stripping nested app titles

Programs running inside the session (e.g. Claude Code, vim) may emit their own
OSC 0/2 title sequences. The daemon strips these from PTY output
(`_strip_osc_title`) and replaces them with the session name. This ensures the
terminal tab always shows the session name, not whatever the inner app set.

The stripped title is still **extracted** (`_extract_osc_title`) and saved to
`~/.sessio/<name>.title` for display in `sessio list`.

### Separate title frame

The session name title is sent as a **separate frame** from the PTY output data.
This prevents corruption when PTY output ends with an incomplete escape sequence
— if concatenated, the terminal parser could consume the `\x1b` from the title
as part of the incomplete sequence, leaving `]0;name` visible as literal text.

```python
output_frame = bytes([TAG_OUTPUT]) + clean_data
title_frame  = bytes([TAG_OUTPUT]) + osc_title
# sent as two separate _send_frame calls
```

### Terminal emulator requirements

The terminal must support dynamic titles (OSC 0). Most do by default, but some
need configuration:

- **Konsole**: Settings → Edit Profile → Tabs → set Tab/Remote title format to `%w`
- **GNOME Terminal**: Usually works; check profile for "dynamic title" setting
- **VS Code / MiniCode**: Always works (built-in OSC support)


## Multi-Client Window Size (SIGWINCH) Logic

### Problem

Multiple clients can attach to the same session simultaneously from different
devices (e.g. desktop PC with 120 cols, phone with 50 cols). The PTY can only
have one size. If it uses the PC's width, the phone sees misaligned output.
If it uses the phone's width, the PC wastes screen space.

### Design: active rows, minimum cols

```
PTY size = (active_client.rows, min(cols across all recent clients))
```

- **Rows**: from the active client. This preserves scrollback depth for the
  device currently being used.
- **Cols**: minimum across all recently-active clients. This ensures no client
  sees line-wrapping artifacts. A line rendered at 50 cols displays correctly
  on both a 50-col and 120-col terminal.

### Active client tracking

The **active client** is the client that most recently sent data:

- Sending a **winsize frame** makes the client active (important: newly
  connecting clients send winsize immediately, so they become active on attach)
- Sending **input data** (keystrokes) makes the client active
- When the active client disconnects, `active_client` is set to `None` until
  another client sends data

Every frame received from a client updates `client_last_active[client]` with
`time.monotonic()`.

### Stale client exclusion

Clients idle for more than **60 seconds** (no frames sent) are excluded from the
min cols calculation. This means when you put down your phone and switch to your
PC, within 60 seconds the session expands to full PC width — even if the phone
is still connected.

The check runs in two places:
- **On client input/winsize** — immediate recalculation when a client sends data
- **On PTY output** (`_read_pty`) — catches stale transitions even when the
  remaining client is passively watching output (not typing). This ensures the
  resize happens as soon as the app produces output after the timeout.

```python
STALE_SECONDS = 60
active_cols = [
    c for client, (_, c) in client_winsize.items()
    if now - client_last_active.get(client, now) < STALE_SECONDS
]
```

New clients that haven't sent any frame yet default to `now` (via `.get(client, now)`)
so they are included immediately.

### Resize triggers

Resize (`_set_winsize`) is called when:

1. **Any client sends a winsize frame** — client becomes active, PTY resizes to
   `(client.rows, min_cols)`
2. **A different client sends input** — that client becomes active, PTY resizes
   to its rows with min cols
3. **A client disconnects** — if the narrowest client left, min cols may
   increase, so the PTY resizes for the remaining active client

### Two-step resize trick

`_set_winsize` does a two-step TIOCSWINSZ to force SIGWINCH even when the size
hasn't changed (e.g. re-attaching from the same terminal):

```python
ioctl(fd, TIOCSWINSZ, (rows, cols-1))  # fake size
sleep(0.5)
ioctl(fd, TIOCSWINSZ, (rows, cols))    # real size
```

The 500ms delay prevents apps like Claude Code from debouncing the two signals
into one no-op.

### State tracked per session

| Field                | Type                              | Purpose                                    |
|----------------------|-----------------------------------|--------------------------------------------|
| `client_winsize`     | `dict[socket, (rows, cols)]`      | Last reported size per client               |
| `client_last_active` | `dict[socket, float]`             | `time.monotonic()` of last frame per client |
| `active_client`      | `socket \| None`                  | Client that last sent data                  |
| `current_winsize`    | `(rows, cols)`                    | Current PTY size (after effective calc)      |


## Connection Lifecycle

### Client attach flow

1. Client connects to Unix socket `~/.sessio/<name>.sock`
2. Daemon sends **scrollback** frame (raw PTY history)
3. Daemon sends **OSC 7** frame (current working directory)
4. Client drains any pending output frames
5. Client sends **winsize frame** → becomes active client → PTY resizes
6. Client enters raw mode, bidirectional pipe begins

### Broken pipe handling

If the daemon has died but its socket file remains, `connect()` succeeds but
the first `sendall` raises `BrokenPipeError`. The client catches this on the
initial winsize send and prints:

```
Session 'name' is dead. Run: sessio kill name
```

### Client disconnect

On disconnect (`_remove_client`):
- Socket closed, removed from `clients` list
- `client_winsize` and `client_last_active` entries cleaned up
- If this was the active client, `active_client` set to `None`
- PTY resized for remaining active client (min cols may have changed)
