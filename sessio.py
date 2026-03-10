#!/usr/bin/env python3
"""sessio - A lightweight terminal session manager."""

import fcntl
import os
import pathlib
import pty
import readline
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

VERSION = "0.1.0"
SESSIO_DIR = pathlib.Path.home() / ".sessio"
MAX_SCROLLBACK_CHUNKS = 10_000
DEFAULT_SCROLLBACK_BYTES = 2048
HISTORY_FILE = SESSIO_DIR / "history"
HISTORY_LENGTH = 50_000

TAG_OUTPUT = 0x00
TAG_SCROLLBACK = 0x01
TAG_WINSIZE = 0x02

DETACH_KEY = 0x1D  # Ctrl+]


def _set_terminal_title(title: str) -> None:
    """Emit OSC 0 to set the terminal emulator's window/tab title."""
    sys.stdout.buffer.write(f"\x1b]0;{title}\x07".encode())
    sys.stdout.buffer.flush()


# ── Wire protocol ──────────────────────────────────────────────────────

def _send_frame(sock: socket.socket, data: bytes) -> None:
    frame = struct.pack("!I", len(data)) + data
    sock.sendall(frame)


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _recv_frame(sock: socket.socket) -> bytes | None:
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack("!I", header)
    if length == 0:
        return b""
    return _recv_exact(sock, length)


def _get_terminal_size() -> tuple[int, int]:
    """Return (rows, cols) of the current terminal."""
    try:
        cols, rows = os.get_terminal_size()
        return rows, cols
    except OSError:
        return 24, 80


def _pack_winsize(rows: int, cols: int) -> bytes:
    return bytes([TAG_WINSIZE]) + struct.pack("!HH", rows, cols)


# ── SessionServer (daemon) ─────────────────────────────────────────────

class SessionServer:
    def __init__(self, name: str):
        self.name = name
        self.sock_path = SESSIO_DIR / f"{name}.sock"
        self.pid_path = SESSIO_DIR / f"{name}.pid"
        self.log_path = SESSIO_DIR / f"{name}.log"
        self.title_path = SESSIO_DIR / f"{name}.title"
        self.scrollback: list[bytes] = []
        self.clients: list[socket.socket] = []
        self.master_fd: int = -1
        self.proc: subprocess.Popen | None = None
        self.srv_sock: socket.socket | None = None
        self._osc_buf: bytearray | None = None  # buffer for partial OSC sequences

    def start(self) -> None:
        SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)

        # Open pty and spawn shell
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        shell = os.environ.get("SHELL", "/bin/sh")
        self.proc = subprocess.Popen(
            [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        # Bind unix socket
        self.srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.sock_path.exists():
            self.sock_path.unlink()
        self.srv_sock.bind(str(self.sock_path))
        os.chmod(str(self.sock_path), 0o600)
        self.srv_sock.listen(5)
        self.srv_sock.setblocking(False)

        # Write PID
        self.pid_path.write_text(str(os.getpid()))

        self._loop()

    def _loop(self) -> None:
        assert self.srv_sock is not None
        try:
            while True:
                # Check if shell exited
                if self.proc and self.proc.poll() is not None:
                    break

                rlist = [self.srv_sock, self.master_fd] + self.clients
                try:
                    readable, _, _ = select.select(rlist, [], [], 1.0)
                except (ValueError, OSError):
                    # Bad fd in list, clean up dead clients
                    self._purge_dead_clients()
                    continue

                for fd in readable:
                    if fd is self.srv_sock:
                        self._accept_client()
                    elif fd is self.master_fd:
                        self._read_pty()
                    else:
                        self._read_client(fd)
        finally:
            self._cleanup()

    def _accept_client(self) -> None:
        assert self.srv_sock is not None
        try:
            conn, _ = self.srv_sock.accept()
        except OSError:
            return
        # Send scrollback dump
        dump = b"".join(self.scrollback)
        _send_frame(conn, bytes([TAG_SCROLLBACK]) + dump)
        # Send CWD info and OSC 7 for the terminal emulator
        if self.proc:
            try:
                proc_cwd = pathlib.Path(f"/proc/{self.proc.pid}/cwd")
                if proc_cwd.exists():
                    cwd = str(proc_cwd.resolve())
                else:
                    # Fallback for non-Linux (macOS, BSD)
                    import shutil
                    if shutil.which("lsof"):
                        result = subprocess.run(
                            ["lsof", "-a", "-p", str(self.proc.pid), "-d", "cwd", "-Fn"],
                            capture_output=True, text=True, timeout=2,
                        )
                        cwd = None
                        for line in result.stdout.splitlines():
                            if line.startswith("n"):
                                cwd = line[1:]
                                break
                    else:
                        cwd = None
                if cwd:
                    hostname = socket.gethostname()
                    osc7 = f"\x1b]7;file://{hostname}{cwd}\x07".encode()
                    _send_frame(conn, bytes([TAG_OUTPUT]) + osc7)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.clients.append(conn)

    def _extract_osc_title(self, data: bytes) -> None:
        """Scan pty output for OSC 0/2 title sequences and save to file."""
        for byte in data:
            if self._osc_buf is not None:
                buflen = len(self._osc_buf)
                if buflen == 1:
                    # We have ESC, expecting ]
                    if byte == 0x5D:  # ]
                        self._osc_buf.append(byte)
                    else:
                        self._osc_buf = None
                        # This byte could be a new ESC
                        if byte == 0x1B:
                            self._osc_buf = bytearray([byte])
                else:
                    self._osc_buf.append(byte)
                    # BEL terminates OSC
                    if byte == 0x07:
                        self._finish_osc_title()
                    # ESC \ (ST) terminates OSC
                    elif byte == 0x5C and buflen >= 2 and self._osc_buf[-2] == 0x1B:
                        self._finish_osc_title()
                    # Abandon if too long
                    elif buflen > 512:
                        self._osc_buf = None
            elif byte == 0x1B:
                self._osc_buf = bytearray([byte])

    def _finish_osc_title(self) -> None:
        """Parse completed OSC buffer and save title if it's OSC 0 or 2."""
        buf = self._osc_buf
        self._osc_buf = None
        if buf is None:
            return
        # Strip terminator (BEL or ESC \)
        if buf[-1] == 0x07:
            content = buf[2:-1]  # skip ESC ]
        elif buf[-2:] == b'\x1b\\':
            content = buf[2:-2]
        else:
            return
        # Check for OSC 0 or OSC 2 (both set window title)
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            return
        if text.startswith("0;") or text.startswith("2;"):
            title = text[2:]
            try:
                self.title_path.write_text(title)
            except OSError:
                pass

    def _read_pty(self) -> None:
        try:
            data = os.read(self.master_fd, 4096)
        except OSError:
            return
        if not data:
            return
        # Extract terminal title from OSC sequences
        self._extract_osc_title(data)
        # Store in scrollback
        self.scrollback.append(data)
        while len(self.scrollback) > MAX_SCROLLBACK_CHUNKS:
            self.scrollback.pop(0)
        # Broadcast to clients, always enforcing session name as terminal title
        osc_title = f"\x1b]0;{self.name}\x07".encode()
        frame_data = bytes([TAG_OUTPUT]) + data + osc_title
        dead = []
        for client in self.clients:
            try:
                _send_frame(client, frame_data)
            except OSError:
                dead.append(client)
        for client in dead:
            self._remove_client(client)

    def _read_client(self, client: socket.socket) -> None:
        data = _recv_frame(client)
        if data is None:
            self._remove_client(client)
            return
        # Check for winsize frame
        if data and data[0] == TAG_WINSIZE and len(data) == 5:
            rows, cols = struct.unpack("!HH", data[1:5])
            self._set_winsize(rows, cols)
            return
        # Write raw input to pty
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def _set_winsize(self, rows: int, cols: int) -> None:
        try:
            # Two-step resize: briefly set different cols then restore.
            # This forces SIGWINCH even when the size hasn't changed
            # (e.g. on re-attach). The 500ms delay prevents apps like
            # Claude Code from debouncing the two signals into one.
            fake = struct.pack("HHHH", rows, cols - 1, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, fake)
            time.sleep(0.5)
            real = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, real)
        except OSError:
            pass

    def _remove_client(self, client: socket.socket) -> None:
        try:
            client.close()
        except OSError:
            pass
        if client in self.clients:
            self.clients.remove(client)

    def _purge_dead_clients(self) -> None:
        dead = []
        for client in self.clients:
            try:
                client.fileno()
            except Exception:
                dead.append(client)
        for client in dead:
            self._remove_client(client)

    def _cleanup(self) -> None:
        for client in list(self.clients):
            self._remove_client(client)
        if self.srv_sock:
            try:
                self.srv_sock.close()
            except OSError:
                pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        if self.sock_path.exists():
            self.sock_path.unlink()
        if self.pid_path.exists():
            self.pid_path.unlink()
        if self.title_path.exists():
            self.title_path.unlink()


# ── RawClient (default — full pty forwarding) ─────────────────────────

class RawClient:
    """Raw-mode client: transparent pipe between user terminal and pty."""

    def __init__(self, name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES):
        self.name = name
        self.sock_path = SESSIO_DIR / f"{name}.sock"
        self.sock: socket.socket | None = None
        self.scrollback_bytes = scrollback_bytes
        self.old_termios: list | None = None
        self.running = False

    def run(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.sock_path))

        # Set terminal title to session name
        _set_terminal_title(self.name)

        # Receive scrollback
        data = _recv_frame(self.sock)
        if data and len(data) > 1 and data[0] == TAG_SCROLLBACK:
            payload = data[1:]
            if self.scrollback_bytes != 0 and payload:
                if self.scrollback_bytes > 0 and len(payload) > self.scrollback_bytes:
                    payload = payload[-self.scrollback_bytes:]
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()

        # Drain any pending frames (e.g. CWD info) before raw mode
        self.sock.setblocking(False)
        try:
            while True:
                frame = _recv_frame(self.sock)
                if frame is None:
                    break
                if len(frame) > 1 and frame[0] == TAG_OUTPUT:
                    sys.stdout.buffer.write(frame[1:])
                    sys.stdout.buffer.flush()
        except (BlockingIOError, OSError):
            pass
        self.sock.setblocking(True)

        time.sleep(0.5)

        # Send initial terminal size
        rows, cols = _get_terminal_size()
        _send_frame(self.sock, _pack_winsize(rows, cols))

        # Set up SIGWINCH handler
        prev_sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._handle_sigwinch)

        # Enter raw mode
        stdin_fd = sys.stdin.fileno()
        self.old_termios = termios.tcgetattr(stdin_fd)
        self.running = True
        try:
            tty.setraw(stdin_fd)
            self._raw_loop(stdin_fd)
        finally:
            self.running = False
            # Restore terminal
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, self.old_termios)
            signal.signal(signal.SIGWINCH, prev_sigwinch)
            _set_terminal_title("")  # clear title on detach
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            print("\r[detached]")

    def _raw_loop(self, stdin_fd: int) -> None:
        assert self.sock is not None
        sock_fd = self.sock.fileno()

        while self.running:
            try:
                readable, _, _ = select.select([stdin_fd, sock_fd], [], [], 1.0)
            except (ValueError, OSError):
                break

            for fd in readable:
                if fd == stdin_fd:
                    try:
                        data = os.read(stdin_fd, 4096)
                    except OSError:
                        self.running = False
                        break
                    if not data:
                        self.running = False
                        break
                    # Check for detach key (Ctrl+])
                    if DETACH_KEY in data:
                        # If detach key is the only byte, detach
                        # If mixed with other data, send everything before it
                        idx = data.index(DETACH_KEY)
                        if idx > 0:
                            try:
                                _send_frame(self.sock, data[:idx])
                            except OSError:
                                pass
                        self.running = False
                        break
                    try:
                        _send_frame(self.sock, data)
                    except OSError:
                        self.running = False
                        break
                elif fd == sock_fd:
                    frame = _recv_frame(self.sock)
                    if frame is None:
                        # Server disconnected
                        self.running = False
                        sys.stdout.buffer.write(b"\r\n[session ended]\r\n")
                        sys.stdout.buffer.flush()
                        break
                    if len(frame) < 1:
                        continue
                    tag = frame[0]
                    payload = frame[1:]
                    if tag == TAG_OUTPUT:
                        sys.stdout.buffer.write(payload)
                        sys.stdout.buffer.flush()

    def _handle_sigwinch(self, signum: int, frame: object) -> None:
        if self.sock and self.running:
            rows, cols = _get_terminal_size()
            try:
                _send_frame(self.sock, _pack_winsize(rows, cols))
            except OSError:
                pass


# ── LineClient (legacy readline mode) ─────────────────────────────────

class LineClient:
    def __init__(self, name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES):
        self.name = name
        self.sock_path = SESSIO_DIR / f"{name}.sock"
        self.sock: socket.socket | None = None
        self.stop_event = threading.Event()
        self.scrollback_bytes = scrollback_bytes

    def run(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.sock_path))

        # Set terminal title to session name
        _set_terminal_title(self.name)

        # Receive scrollback
        data = _recv_frame(self.sock)
        if data and len(data) > 1 and data[0] == TAG_SCROLLBACK:
            payload = data[1:]
            if self.scrollback_bytes != 0 and payload:
                if self.scrollback_bytes > 0 and len(payload) > self.scrollback_bytes:
                    payload = payload[-self.scrollback_bytes:]
                print("─── scrollback ───")
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()

        print(f"[attached: {self.name}]")

        self._setup_history()

        # Start reader thread
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        reader.start()

        # Input loop
        try:
            while not self.stop_event.is_set():
                try:
                    line = input()
                except EOFError:
                    _set_terminal_title("")
                    print("\n[detached]")
                    break
                except KeyboardInterrupt:
                    if self.sock:
                        try:
                            _send_frame(self.sock, b"\x03")
                        except OSError:
                            break
                    continue
                if self.stop_event.is_set():
                    break
                try:
                    _send_frame(self.sock, (line + "\n").encode())
                except OSError:
                    break
        finally:
            self._save_history()
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass

    def _reader_loop(self) -> None:
        assert self.sock is not None
        while not self.stop_event.is_set():
            data = _recv_frame(self.sock)
            if data is None:
                self.stop_event.set()
                print("\n[session ended]")
                break
            if len(data) < 1:
                continue
            tag = data[0]
            payload = data[1:]
            if tag == TAG_OUTPUT:
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()

    def _setup_history(self) -> None:
        SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
        readline.parse_and_bind("tab: complete")
        try:
            readline.read_history_file(str(HISTORY_FILE))
        except FileNotFoundError:
            pass
        readline.set_history_length(HISTORY_LENGTH)

    def _save_history(self) -> None:
        try:
            readline.write_history_file(str(HISTORY_FILE))
        except OSError:
            pass


# ── Daemonize ──────────────────────────────────────────────────────────

def daemonize(server: SessionServer) -> None:
    """Double-fork to detach daemon process."""
    pid = os.fork()
    if pid > 0:
        return

    # First child
    os.setsid()

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Second child — the actual daemon
    log_fd = os.open(str(server.log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    os.close(log_fd)

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        server.start()
    except Exception as e:
        sys.stderr.write(f"daemon error: {e}\n")
    finally:
        os._exit(0)


# ── CLI commands ───────────────────────────────────────────────────────

def cmd_new(name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES, line_mode: bool = False) -> None:
    SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
    pid_path = SESSIO_DIR / f"{name}.pid"
    sock_path = SESSIO_DIR / f"{name}.sock"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            print(f"error: session '{name}' already exists (pid {pid})", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pid_path.unlink(missing_ok=True)
            sock_path.unlink(missing_ok=True)

    if sock_path.exists() and not pid_path.exists():
        sock_path.unlink()

    server = SessionServer(name)
    daemonize(server)

    for _ in range(20):
        if sock_path.exists():
            break
        time.sleep(0.1)
    else:
        print(f"error: daemon failed to start for '{name}'", file=sys.stderr)
        sys.exit(1)

    cmd_attach(name, scrollback_bytes=scrollback_bytes, line_mode=line_mode)


def cmd_attach(name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES, line_mode: bool = False) -> None:
    sock_path = SESSIO_DIR / f"{name}.sock"
    if not sock_path.exists():
        print(f"error: no session named '{name}'", file=sys.stderr)
        sys.exit(1)
    if line_mode:
        client = LineClient(name, scrollback_bytes=scrollback_bytes)
    else:
        client = RawClient(name, scrollback_bytes=scrollback_bytes)
    client.run()


def cmd_list() -> None:
    SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
    pid_files = sorted(SESSIO_DIR.glob("*.pid"))
    if not pid_files:
        print("no active sessions")
        return
    for pf in pid_files:
        name = pf.stem
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)
            title_path = SESSIO_DIR / f"{name}.title"
            title = ""
            try:
                title = title_path.read_text().strip()
            except (OSError, FileNotFoundError):
                pass
            if title:
                print(f"  {name} (pid {pid}) — {title}")
            else:
                print(f"  {name} (pid {pid})")
        except (ProcessLookupError, ValueError):
            print(f"  {name} (stale)")
            pf.unlink(missing_ok=True)
            sock = SESSIO_DIR / f"{name}.sock"
            sock.unlink(missing_ok=True)
            title = SESSIO_DIR / f"{name}.title"
            title.unlink(missing_ok=True)


def cmd_kill(name: str) -> None:
    pid_path = SESSIO_DIR / f"{name}.pid"
    if not pid_path.exists():
        print(f"error: no session named '{name}'", file=sys.stderr)
        sys.exit(1)
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"killed session '{name}' (pid {pid})")
    except ProcessLookupError:
        print(f"session '{name}' was already dead, cleaning up")
    except ValueError:
        print(f"error: corrupt pid file for '{name}'", file=sys.stderr)
    pid_path.unlink(missing_ok=True)
    sock_path = SESSIO_DIR / f"{name}.sock"
    sock_path.unlink(missing_ok=True)
    title_path = SESSIO_DIR / f"{name}.title"
    title_path.unlink(missing_ok=True)


# ── Main ───────────────────────────────────────────────────────────────

USAGE = """\
usage: sessio <command> [args]

commands:
  new <name> [-s BYTES] [--line]    create a new session and attach
  attach <name> [-s BYTES] [--line] attach to an existing session
  list                              list active sessions
  kill <name>                       kill a session

options:
  -s, --scrollback BYTES   scrollback bytes on attach (default: 2048, 0=none, -1=all)
  --line                   use line mode (readline) instead of raw mode

Raw mode (default) supports TUI programs (vim, htop, claude).
Detach with Ctrl+].  Line mode detaches with Ctrl+D."""


def _parse_scrollback(args: list[str]) -> int:
    for i, a in enumerate(args):
        if a in ("-s", "--scrollback") and i + 1 < len(args):
            val = int(args[i + 1])
            return val if val >= 0 else -1
    return DEFAULT_SCROLLBACK_BYTES


def _parse_line_mode(args: list[str]) -> bool:
    return "--line" in args


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(1)

    cmd = args[0]
    rest = args[1:]

    if cmd in ("-v", "--version"):
        print(f"sessio {VERSION}")
        return

    if cmd == "new":
        if not rest or rest[0].startswith("-"):
            print("usage: sessio new <name> [-s BYTES] [--line]", file=sys.stderr)
            sys.exit(1)
        sb = _parse_scrollback(rest[1:])
        line_mode = _parse_line_mode(rest[1:])
        cmd_new(rest[0], scrollback_bytes=sb, line_mode=line_mode)
    elif cmd == "attach":
        if not rest or rest[0].startswith("-"):
            print("usage: sessio attach <name> [-s BYTES] [--line]", file=sys.stderr)
            sys.exit(1)
        sb = _parse_scrollback(rest[1:])
        line_mode = _parse_line_mode(rest[1:])
        cmd_attach(rest[0], scrollback_bytes=sb, line_mode=line_mode)
    elif cmd == "list":
        cmd_list()
    elif cmd == "kill":
        if len(rest) < 1:
            print("usage: sessio kill <name>", file=sys.stderr)
            sys.exit(1)
        cmd_kill(rest[0])
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
