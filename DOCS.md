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

### Auto-create on attach

`sessio attach <name>` auto-creates the session if it doesn't exist, making it
idempotent. This simplifies scripts and aliases — no need to check first.

### Client disconnect

On disconnect (`_remove_client`):
- Socket closed, removed from `clients` list
- `client_winsize` and `client_last_active` entries cleaned up
- If this was the active client, `active_client` set to `None`
- PTY resized for remaining active client (min cols may have changed)


## OSC 7 CWD Tracking

The daemon extracts OSC 7 sequences (`\e]7;file://hostname/path\a`) from PTY
output and writes the path to `~/.sessio/<name>.cwd`. This is used by:

- `sessio list` — shows CWD next to each session
- `sessio menu` — shows CWD in the interactive picker
- MiniCode — updates SFTP file tree to match terminal directory

Shell integration (added by `sessio install`) emits OSC 7 on every prompt:

```bash
# bash
__sessio_osc7() { printf '\e]7;file://%s%s\a' "$HOSTNAME" "$PWD"; }
PROMPT_COMMAND="__sessio_osc7${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
```


## Sandbox Mode (bwrap)

`sessio sandbox` runs Claude Code inside a bubblewrap container with minimal
filesystem access. Linux only (requires kernel namespaces).

### What gets isolated

- **Read-write**: project directory, `~/.claude` config
- **Read-only**: system libs (`/usr`), configured tool paths
- **Isolated**: `/home` (except above), `/tmp`, `/var`
- **Network**: shared (not isolated)

### Configuration

Paths are configured in `~/.sessio/sandbox.conf` (INI format, created by
`sessio install`). Key sections:

```ini
[sandbox]
command = claude --dangerously-skip-permissions
rw_paths = %(home)s/.claude
ro_paths = %(home)s/.nvm
           %(home)s/.local/bin
extra_path = %(home)s/.local/bin
env = CLAUDE_CONFIG_DIR=%(home)s/.claude
sessions_map = %(home)s/.claude/sandbox-sessions
```

`%(home)s` expands to `$HOME`. Missing paths are silently skipped.

### Session resolution (`cs <arg>`)

The `cs` alias (set up by `sessio install`) maps to `sessio sandbox`:

1. Existing session name → attach (reuse running session)
2. Absolute path that exists → `name=basename(path)`, `dir=path`
3. Otherwise → `name=arg`, `dir=cwd`
4. No args → `name=basename(cwd)`, `dir=cwd`

### Claude session resume

When using Claude, sessio tracks session UUIDs in a mapping file
(`~/.claude/sandbox-sessions`). On next launch with the same session name,
it passes `-r <uuid>` to resume the conversation.

### bwrap wrapper

Sessio creates a transient wrapper script at `~/.sessio/<name>.wrapper.sh`
that launches bwrap with the configured paths. The wrapper sets `$SHELL` and
is passed to `sessio new`, which spawns it as the session's shell. The wrapper
deletes itself on launch.

### Requirements

```bash
sudo apt install bubblewrap    # Debian/Ubuntu
```


## Interactive Menu

`sessio menu` provides a numbered session picker for quick access on login.

### VPN gating

When called with `--vpn`, only shows the menu for connections from allowed IPs:

```bash
# In .bashrc
sessio menu --vpn 10.10.10.21,10.10.10.22
```

Non-VPN connections silently skip the menu and drop to normal shell.

### Session ordering

Sessions are sorted by socket mtime (most recently used first).

### Selection

- ≤9 sessions: single keypress (no Enter needed)
- >9 sessions: type number + Enter
- `q`: exit to normal shell


## Install

`sessio install` is an interactive setup wizard:

1. **Binary** — symlinks `sessio.py` to `~/.local/bin/sessio` or
   `/usr/local/bin/sessio`
2. **Shell integration** — appends OSC 7 + `cs` alias to `~/.bashrc`/`~/.zshrc`
3. **Sandbox config** — creates `~/.sessio/sandbox.conf` with defaults


## File Layout

```
~/.sessio/
  sandbox.conf             bwrap path configuration
  <name>.sock              Unix domain socket
  <name>.pid               daemon PID
  <name>.log               daemon stderr
  <name>.title             last OSC window title
  <name>.cwd               last working directory (from OSC 7)
  <name>.wrapper.sh        transient bwrap launcher (auto-deleted)
  history                  shared readline history (line mode)
```
