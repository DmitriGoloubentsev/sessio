# sessio

Persistent terminal sessions. Pure Python, zero dependencies.

Sessions survive disconnects, terminal closures, and SSH drops. Full TUI support (vim, htop, claude). Multiple clients can attach simultaneously. Connect from your phone, detach, pick up on your PC.

## Quick start

```bash
git clone https://github.com/DmitriGoloubentsev/sessio.git
cd sessio
./sessio.py install
```

The installer asks three questions:

1. **Where to install the binary** — `~/.local/bin` (user) or `/usr/local/bin` (system, sudo)
2. **Add shell integration to .bashrc/.zshrc?** — adds `cs` alias, CWD tracking, session list on SSH login
3. **Create sandbox config?** — `~/.sessio/sandbox.conf` for bwrap paths (Claude Code sandbox)

After install, restart your shell or `source ~/.bashrc`.

## Usage

```bash
sessio new dev           # create session "dev" and attach
sessio attach dev        # re-attach (creates if needed)
sessio list              # show active sessions with CWD
sessio kill dev          # kill session
sessio kill              # kill all (with confirmation)
```

**Detach with Ctrl+]** — the session keeps running. Re-attach anytime.

## Claude Code sandbox

Run Claude Code in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox. Claude gets `--dangerously-skip-permissions` but can only touch the project directory.

```bash
cs                        # session=dirname, dir=cwd
cs myproject              # attach if exists, else create
cs myproject /path/to/dir # explicit name and directory
cs --shell                # bash instead of claude
```

Requires: `sudo apt install bubblewrap`

Configure sandbox paths in `~/.sessio/sandbox.conf`. Missing paths are silently skipped.

## Session menu

Interactive session picker:

```bash
sessio menu              # numbered list, single-keypress select
```

On SSH login, shell integration prints available sessions automatically. Type `cs <name>` to jump in.

## Multi-client

Multiple terminals can attach to the same session. Sessio uses the minimum columns across active clients so all screens render correctly. Clients idle >60 seconds are excluded — put your phone down and the PC expands to full width.

## Remote access

sessio uses Unix sockets (local only). For remote access, SSH in and attach:

```bash
ssh user@server
sessio attach dev
```

Use [Tailscale](https://tailscale.com/) for easy VPN access from mobile without port forwarding.

## Platform support

- **Linux** — full support including sandbox
- **macOS / BSD** — core features work, no sandbox
- **Termux** — works, `--line` mode recommended for mobile keyboards

## All commands

```
sessio new <name> [opts]              create session and attach
sessio attach <name> [opts]           attach (creates if needed)
sessio list                           list sessions with CWD
sessio kill [name...]                 kill sessions (no args = all)
sessio sandbox [--shell] [name] [dir] claude in bwrap (alias: cs)
sessio menu [--vpn IPs]              session picker
sessio install                        install to PATH + shell setup
sessio uninstall                      remove everything
sessio -h / -v                        help / version

Options:
  -s, --scrollback BYTES   scrollback on attach (default: 2048, 0=none, -1=all)
  --line                   readline mode for mobile keyboards
```
