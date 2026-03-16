# sessio

A lightweight terminal session manager. Pure Python, zero dependencies.

Persistent shell sessions that survive terminal closures, with full raw-mode pty support for TUI programs (vim, htop, claude), an optional sandbox for running Claude Code safely, and an interactive session picker for mobile access.

## Features

- **Session persistence** — shell sessions survive closing the terminal
- **Raw mode (default)** — full pty forwarding, TUI programs work correctly
- **Line mode** — optional readline-based input for mobile keyboards
- **Scrollback replay** — configurable output history shown on re-attach
- **Terminal title** — auto-detects OSC title sequences; sets tab name on attach
- **CWD tracking** — extracts OSC 7 sequences; shows working directory in `sessio list`
- **SIGWINCH propagation** — terminal resize forwarded to the session
- **Multi-client** — multiple clients can attach to the same session simultaneously
- **Smart resize** — uses minimum columns across active clients; stale clients (idle >60s) excluded
- **Sandbox mode** — run Claude Code in a bwrap container with minimal filesystem access
- **Session menu** — interactive picker for quick access on SSH login
- **Zero dependencies** — stdlib only, Python 3.10+
- **Per-user isolation** — socket and state files are owner-only (`0600`/`0700`)

## Install

```bash
pip install sessio
```

Or install from source with the interactive installer:

```bash
git clone https://github.com/DmitriGoloubentsev/sessio.git
cd sessio
python3 sessio.py install
```

This will:
1. Symlink `sessio` into your PATH (`~/.local/bin` or `/usr/local/bin`)
2. Add shell integration to `~/.bashrc` or `~/.zshrc` (OSC 7 CWD tracking + `cs` alias)
3. Create `~/.sessio/sandbox.conf` with default bwrap paths

Manual install (without the wizard):

```bash
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
sessio new <name>                    # create a new session and attach
sessio attach <name>                 # attach to session (creates if needed)
sessio list                          # list active sessions with CWD
sessio kill <name> [name2 ...]       # kill one or more sessions
sessio kill                          # kill all sessions (with confirmation)
sessio sandbox [name] [dir]          # claude in bwrap sandbox (alias: cs)
sessio menu [--vpn IPs]              # interactive session picker
sessio install                       # install to PATH and configure shell
sessio -v                            # show version
```

### Options

```
-s, --scrollback BYTES   scrollback bytes on attach (default: 2048, 0=none, -1=all)
--line                   use line mode (readline) instead of raw mode
-h, --help               show help
-v, --version            show version
```

### Examples

```bash
sessio new dev                   # start a new session called "dev"
sessio attach dev -s 8192        # re-attach with 8KB scrollback
sessio attach dev -s -1          # re-attach with full scrollback
sessio attach myproject          # creates "myproject" if it doesn't exist
sessio new mobile --line         # line mode for mobile keyboards
sessio kill dev staging          # kill multiple sessions
```

## Detaching

- **Raw mode (default):** press **Ctrl+]**
- **Line mode (`--line`):** press **Ctrl+D**

The session continues running in the background. Re-attach with `sessio attach <name>`.

## Sandbox mode (bwrap)

Run Claude Code inside a [bubblewrap](https://github.com/containers/bubblewrap) container with restricted filesystem access. This lets you use `--dangerously-skip-permissions` safely — Claude can only modify files in the project directory.

```bash
# Using the cs alias (set up by sessio install)
cs                        # session=dirname, dir=cwd, runs claude
cs myproject              # attach if exists, else create with dir=cwd
cs myproject /path/to/dir # explicit name and directory
cs --shell                # bash instead of claude

# Or directly
sessio sandbox myproject /opt/dev/myproject
```

### What gets sandboxed

| Access    | Paths                                           |
|-----------|--------------------------------------------------|
| Read-write | Project directory, `~/.claude`                  |
| Read-only  | `/usr`, configured tool paths (node, python, etc) |
| Isolated   | `/home` (except above), `/tmp`, `/var`          |
| Network    | Shared (not isolated)                           |

### Configuration

Edit `~/.sessio/sandbox.conf` to configure paths for your system:

```ini
[sandbox]
command = claude --dangerously-skip-permissions

rw_paths =
    %(home)s/.claude

ro_paths =
    %(home)s/.nvm
    %(home)s/.local/bin
    %(home)s/.local/share/claude
    %(home)s/.gitconfig
    /opt/tools

extra_path =
    %(home)s/.local/bin

env =
    CLAUDE_CONFIG_DIR=%(home)s/.claude
```

`%(home)s` expands to `$HOME`. Missing paths are silently skipped.

### Requirements

```bash
sudo apt install bubblewrap    # Debian/Ubuntu
```

Sandbox mode is Linux-only (requires kernel namespaces). On other platforms, use `sessio new` directly.

### Claude session resume

Sessio tracks Claude conversation UUIDs in `~/.claude/sandbox-sessions`. When you run `cs myproject` again, it automatically resumes the previous conversation with `claude -r <uuid>`.

## Session list on SSH login

Shell integration (added by `sessio install`) prints available sessions on every SSH login:

```
Available sessions:
  myproject (pid 12345)  /opt/dev/myproject
  api-server (pid 12346)  /opt/dev/api
  Type "sessio menu" or "cs <name>" to connect
```

This serves two purposes:
1. **Human users** — see what's running and type `cs myproject` or `sessio menu`
2. **MiniCode** — detects the session list in terminal output and can show a native UI picker, then send `sessio attach <name>` automatically

No special SSH configuration or environment variables needed — MiniCode just parses the text output.

## Session menu

Interactive session picker, useful from the command line or `.bashrc`:

```bash
# VPN-gated — only show for connections from these IPs
sessio menu --vpn 10.10.10.21,10.10.10.22

# Show unconditionally
sessio menu
```

Output:
```
  Sessio Sessions

  1  myproject            /opt/dev/myproject
  2  api-server           /opt/dev/api
  3  docs                 /opt/dev/docs
  q  Exit

  Select:
```

Single keypress for ≤9 sessions (no Enter needed). Press `q` for normal shell.

## Multi-client resize

Multiple terminals can attach to the same session simultaneously. Sessio uses smart resize logic:

- **Columns**: minimum across all recently-active clients, so no screen sees wrapping artifacts
- **Rows**: from the active client (the one currently typing)
- **Stale exclusion**: clients idle for >60 seconds are excluded from the min-cols calculation

This means you can monitor a session from your phone while working on your PC. When you put the phone down, within 60 seconds the session expands to full PC width.

## Terminal title

sessio sets the terminal tab title to the session name on attach, and strips title sequences from programs running inside (e.g. Claude Code), replacing them with the session name. The program's title is still captured and shown in `sessio list`.

## CWD tracking

sessio extracts [OSC 7](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html) sequences from program output and saves the working directory to `~/.sessio/<name>.cwd`. This is used by `sessio list`, `sessio menu`, and compatible terminal apps like [MiniCode](https://minicode.app) (which updates its SFTP file tree automatically).

Shell integration (added by `sessio install`) emits OSC 7 on every prompt:

```bash
# Bash
__sessio_osc7() { printf '\e]7;file://%s%s\a' "$HOSTNAME" "$PWD"; }
PROMPT_COMMAND="__sessio_osc7${PROMPT_COMMAND:+;$PROMPT_COMMAND}"

# Zsh
chpwd() { printf '\e]7;file://%s%s\a' "$HOST" "$PWD" }
```

## How it works

`sessio new` double-forks a daemon that owns a pty and listens on a Unix socket at `~/.sessio/<name>.sock`. Clients connect to the socket, receive a scrollback dump, then enter interactive mode.

In **raw mode** (default), the client puts the terminal into raw mode and acts as a transparent pipe between stdin/stdout and the pty. All escape sequences, control characters, and TUI rendering pass through unchanged. Terminal resize events (SIGWINCH) are forwarded to the pty.

In **line mode** (`--line`), the client uses readline for input, providing arrow-key history, Ctrl+R search, and tab completion — useful on mobile keyboards where raw mode may be less convenient.

### File layout

```
~/.sessio/
  sandbox.conf             bwrap path configuration
  <name>.sock              Unix domain socket
  <name>.pid               daemon PID
  <name>.log               daemon stderr
  <name>.title             last OSC window title (if set)
  <name>.cwd               last working directory (from OSC 7)
  <name>.wrapper.sh        transient bwrap launcher (auto-deleted)
  history                  shared readline history (line mode)
```

## Remote access

sessio is local-only by design — it uses Unix sockets, not TCP. To access your sessions remotely (e.g. from your phone), expose your machine's SSH server to the internet, then SSH in and run `sessio attach`.

### Option 1: Tailscale (easiest, no ports to open)

[Tailscale](https://tailscale.com/) creates a private WireGuard VPN between your devices. Install it on both your server and phone, and you get a stable IP that works across NAT, cellular, Wi-Fi changes — no port forwarding needed.

```bash
# On your server
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# From your phone (install Tailscale app + any SSH client)
ssh user@your-server-tailscale-ip
sessio attach dev
```

### Option 2: Cloudflare Tunnel (no open ports, free tier)

[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) exposes your SSH server through Cloudflare's network. Requires a domain name.

```bash
# On your server
cloudflared tunnel create my-tunnel
cloudflared tunnel route dns my-tunnel ssh.example.com
cloudflared tunnel run my-tunnel

# On your client, add to ~/.ssh/config:
# Host ssh.example.com
#   ProxyCommand cloudflared access ssh --hostname %h
```

### Option 3: Port forwarding + dynamic DNS

If you control your router, forward port 22 (or a custom port) to your server's local IP. Use a dynamic DNS service if your public IP changes.

```bash
# From your phone
ssh -p 2222 user@your-public-ip-or-ddns
sessio attach dev
```

### Option 4: Reverse SSH tunnel

If you have a VPS or any server with a public IP, create a reverse tunnel from your NAT'd machine:

```bash
# On your NAT'd server (runs persistently)
ssh -R 2222:localhost:22 user@vps-with-public-ip -N

# From your phone
ssh -p 2222 user@vps-with-public-ip
sessio attach dev
```

> **Tip:** Use [autossh](https://www.harding.motd.ca/autossh/) to keep reverse tunnels alive automatically.

### Security recommendations

- Use **SSH key authentication** and disable password login
- Use a **non-standard port** to reduce scan noise
- Consider **fail2ban** to block brute-force attempts
- Tailscale or Cloudflare Tunnel are preferred over exposing ports directly

## Platform support

- **Linux** — full support including sandbox mode and CWD detection via `/proc`
- **macOS / BSD** — core features work; CWD detection falls back to `lsof`; sandbox not available
- **Termux (Android)** — works; line mode recommended for mobile keyboards
- **Windows** — not supported (requires Unix pty and sockets)

## Limitations

- Shared readline history across sessions (line mode only)
- No split panes or window management
- Sandbox mode requires Linux with bubblewrap installed
