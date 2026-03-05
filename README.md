# sessio

A lightweight, mobile-friendly terminal session manager built in Python.

Persistent shell sessions that survive terminal closures, with full raw-mode pty support for TUI programs (vim, htop, claude) and an optional readline-based line mode for mobile/simple use.

## Features

- **Session persistence** — shell sessions survive closing the terminal
- **Raw mode (default)** — full pty forwarding, TUI programs work correctly
- **Line mode** — optional readline-based input for mobile stability
- **Scrollback replay** — output history shown on re-attach
- **SIGWINCH propagation** — terminal resize forwarded to the session
- **Zero dependencies** — stdlib only, Python 3.10+
- **Per-user isolation** — socket and state files are owner-only (`0600`/`0700`)

## Install

```bash
# symlink into your PATH
sudo ln -s $(pwd)/sessio.py /usr/local/bin/sessio

# or, without sudo — add ~/.local/bin to PATH if not already
mkdir -p ~/.local/bin
ln -s $(pwd)/sessio.py ~/.local/bin/sessio
```

On Termux:

```bash
ln -s $(pwd)/sessio.py ~/.local/bin/sessio
```

## Usage

```
sessio new <name>                # create a new session and attach (raw mode)
sessio attach <name>             # re-attach to an existing session (raw mode)
sessio new <name> --line         # create and attach in line mode (readline)
sessio attach <name> --line      # re-attach in line mode
sessio attach <name> -s 8192    # re-attach with 8KB scrollback
sessio attach <name> -s 0       # re-attach without scrollback
sessio attach <name> -s -1      # re-attach with full scrollback
sessio list                      # list active sessions
sessio kill <name>               # terminate a session
```

- **Ctrl+]** — detach from session in raw mode (session keeps running)
- **Ctrl+D** — detach from session in line mode
- **Ctrl+C** — sends interrupt to the shell

Multiple clients can attach to the same session simultaneously.

## Detaching from a session

To disconnect from a session while keeping it running in the background:

- **Raw mode (default):** press **Ctrl+]**
- **Line mode (`--line`):** press **Ctrl+D**

The session continues running after detach. Reattach with `sessio attach <name>`.

## How it works

`sessio new` double-forks a daemon that owns a pty and listens on a Unix socket at `~/.sessio/<name>.sock`. Clients connect to the socket, receive a scrollback dump, then enter interactive mode.

In **raw mode** (default), the client puts the terminal into raw mode and acts as a transparent pipe between stdin/stdout and the pty. All escape sequences, control characters, and TUI rendering pass through unchanged. Terminal resize events (SIGWINCH) are forwarded to the pty.

In **line mode** (`--line`), the client uses readline for input, providing arrow-key history, Ctrl+R search, and tab completion — useful on mobile keyboards where raw mode may be less convenient.

## Limitations

- Shared readline history across sessions (line mode only)
- No split panes or window management
