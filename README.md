# sessio

A lightweight terminal session manager. Pure Python, zero dependencies.

Persistent shell sessions that survive terminal closures, with full raw-mode pty support for TUI programs (vim, htop, claude) and an optional readline-based line mode for mobile/simple use.

## Features

- **Session persistence** — shell sessions survive closing the terminal
- **Raw mode (default)** — full pty forwarding, TUI programs work correctly
- **Line mode** — optional readline-based input for mobile keyboards
- **Scrollback replay** — configurable output history shown on re-attach
- **Terminal title** — auto-detects OSC title sequences; sets tab name on attach
- **SIGWINCH propagation** — terminal resize forwarded to the session
- **Multi-client** — multiple clients can attach to the same session
- **Zero dependencies** — stdlib only, Python 3.10+
- **Per-user isolation** — socket and state files are owner-only (`0600`/`0700`)

## Install

```bash
pip install sessio
```

Or install from source:

```bash
git clone https://github.com/DmitriGoloubentsev/sessio.git
cd sessio

# symlink into your PATH
sudo ln -s $(pwd)/sessio.py /usr/local/bin/sessio

# or, without sudo — add ~/.local/bin to PATH if not already
mkdir -p ~/.local/bin
ln -s $(pwd)/sessio.py ~/.local/bin/sessio
```

On Termux:

```bash
pip install sessio
# or
ln -s $(pwd)/sessio.py ~/.local/bin/sessio
```

## Usage

```
sessio new <name>                # create a new session and attach
sessio attach <name>             # re-attach to an existing session
sessio list                      # list active sessions
sessio kill <name>               # terminate a session
sessio -v                        # show version
```

### Options

```
-s, --scrollback BYTES   scrollback bytes on attach (default: 2048, 0=none, -1=all)
--line                   use line mode (readline) instead of raw mode
```

### Examples

```bash
sessio new dev                   # start a new session called "dev"
sessio attach dev -s 8192        # re-attach with 8KB scrollback
sessio attach dev -s -1          # re-attach with full scrollback
sessio new mobile --line         # line mode for mobile keyboards
```

## Detaching

- **Raw mode (default):** press **Ctrl+]**
- **Line mode (`--line`):** press **Ctrl+D**

The session continues running in the background. Re-attach with `sessio attach <name>`.

## Terminal title

sessio automatically detects when programs inside the session set the terminal title via OSC escape sequences, and restores the title on re-attach. The title is also shown in `sessio list`.

To set the title from within a session:

```bash
printf '\033]0;My Session\007'
```

To set it automatically from your shell prompt:

```bash
# Bash
PROMPT_COMMAND='printf "\033]0;%s\007" "my-session"'

# Zsh
precmd() { print -Pn "\e]0;my-session\a" }
```

## How it works

`sessio new` double-forks a daemon that owns a pty and listens on a Unix socket at `~/.sessio/<name>.sock`. Clients connect to the socket, receive a scrollback dump, then enter interactive mode.

In **raw mode** (default), the client puts the terminal into raw mode and acts as a transparent pipe between stdin/stdout and the pty. All escape sequences, control characters, and TUI rendering pass through unchanged. Terminal resize events (SIGWINCH) are forwarded to the pty.

In **line mode** (`--line`), the client uses readline for input, providing arrow-key history, Ctrl+R search, and tab completion — useful on mobile keyboards where raw mode may be less convenient.

### File layout

```
~/.sessio/
  <name>.sock    Unix domain socket
  <name>.pid     daemon PID
  <name>.log     daemon stderr
  <name>.title   last OSC window title (if set)
```

## Platform support

- **Linux** — full support including CWD detection via `/proc`
- **macOS / BSD** — works fully; CWD detection falls back to `lsof`
- **Termux (Android)** — works; line mode recommended for mobile keyboards
- **Windows** — not supported (requires Unix pty and sockets)

## Limitations

- Shared readline history across sessions (line mode only)
- No split panes or window management
