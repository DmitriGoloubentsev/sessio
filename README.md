# sessio

A lightweight, mobile-friendly terminal session manager built in Python.

Persistent shell sessions that survive terminal closures, with readline-based input for clean keyboard behaviour on both desktop and mobile (including Termux on Android).

## Features

- **Session persistence** — shell sessions survive closing the terminal
- **Scrollback replay** — full output history shown on re-attach
- **Readline I/O** — arrow-key history, Ctrl+R search, tab completion
- **Mobile stability** — no escape-code artefacts on Android/Termux keyboards
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
sessio new <name>                # create a new session and attach
sessio attach <name>             # re-attach to an existing session
sessio attach <name> -s 8192    # re-attach with 8KB scrollback
sessio attach <name> -s 0       # re-attach without scrollback
sessio attach <name> -s -1      # re-attach with full scrollback
sessio list                      # list active sessions
sessio kill <name>               # terminate a session
```

- **Ctrl+D** — detach from session (session keeps running)
- **Ctrl+C** — sends interrupt to the shell

Multiple clients can attach to the same session simultaneously.

## How it works

`sessio new` double-forks a daemon that owns a pty and listens on a Unix socket at `~/.sessio/<name>.sock`. Clients connect to the socket, receive a scrollback dump, then enter an interactive readline loop. Input is forwarded to the pty; output is broadcast to all connected clients.

## Limitations

- **Line-mode only** — interactive TUI programs (vim, htop, less) won't render correctly
- Shared readline history across sessions
- No split panes or window management
- No terminal resize (SIGWINCH) propagation
