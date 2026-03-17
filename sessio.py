#!/usr/bin/env python3
"""sessio - A lightweight terminal session manager."""
from __future__ import annotations

import configparser
import fcntl
import os
import pathlib
import pty
import readline
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

VERSION = "0.3.0"
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
        self.cwd_path = SESSIO_DIR / f"{name}.cwd"
        self.scrollback: list[bytes] = []
        self.clients: list[socket.socket] = []
        self.client_winsize: dict[socket.socket, tuple[int, int]] = {}
        self.client_last_active: dict[socket.socket, float] = {}
        self.active_client: socket.socket | None = None
        self.current_winsize: tuple[int, int] = (24, 80)
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
        env = os.environ.copy()
        env["SESSIO_SESSION"] = self.name
        self.proc = subprocess.Popen(
            [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
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
        try:
            _send_frame(conn, bytes([TAG_SCROLLBACK]) + dump)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            return
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
        elif text.startswith("7;"):
            # OSC 7: CWD notification — file://hostname/path
            uri = text[2:]
            # Strip file://hostname prefix to get path
            if uri.startswith("file://"):
                path_part = uri[7:]  # remove "file://"
                # Skip hostname (everything up to next /)
                slash = path_part.find("/")
                if slash >= 0:
                    cwd = path_part[slash:]
                    try:
                        self.cwd_path.write_text(cwd)
                    except OSError:
                        pass

    @staticmethod
    def _strip_osc_title(data: bytes) -> bytes:
        """Remove OSC 0 and OSC 2 (set title) sequences from pty output.

        This prevents nested applications (e.g. Claude Code) from overriding
        the sessio session name shown in client terminal tabs.
        """
        result = bytearray()
        i = 0
        n = len(data)
        while i < n:
            # Check for ESC ] (OSC start)
            if data[i] == 0x1B and i + 1 < n and data[i + 1] == 0x5D:
                j = i + 2
                cmd_start = j
                # Read OSC command number (digits)
                while j < n and 0x30 <= data[j] <= 0x39:
                    j += 1
                if j < n and data[j] == 0x3B:  # semicolon after cmd number
                    cmd_num = int(data[cmd_start:j]) if j > cmd_start else -1
                    if cmd_num in (0, 2):
                        # Title-setting OSC — skip until BEL or ST terminator
                        j += 1
                        while j < n:
                            if data[j] == 0x07:
                                j += 1
                                break
                            if data[j] == 0x1B and j + 1 < n and data[j + 1] == 0x5C:
                                j += 2
                                break
                            j += 1
                        i = j
                        continue
                # Not a title OSC — keep the bytes
                result.extend(data[i:j])
                i = j
                continue
            result.append(data[i])
            i += 1
        return bytes(result)

    def _read_pty(self) -> None:
        try:
            data = os.read(self.master_fd, 4096)
        except OSError:
            return
        if not data:
            return
        # Check if stale clients changed the effective size
        if self.active_client and self.active_client in self.client_winsize:
            rows, cols = self.client_winsize[self.active_client]
            eff_rows, eff_cols = self._effective_winsize(rows, cols)
            if (eff_rows, eff_cols) != self.current_winsize:
                self._set_winsize(eff_rows, eff_cols)
        # Extract terminal title from OSC sequences (for `sessio list`)
        self._extract_osc_title(data)
        # Store raw data in scrollback
        self.scrollback.append(data)
        while len(self.scrollback) > MAX_SCROLLBACK_CHUNKS:
            self.scrollback.pop(0)
        # Strip OSC title sequences from nested apps, then send our own
        # as a separate frame to avoid corrupting the terminal parser if
        # clean_data ends with an incomplete escape sequence.
        clean_data = self._strip_osc_title(data)
        osc_title = f"\x1b]0;{self.name}\x07".encode()
        output_frame = bytes([TAG_OUTPUT]) + clean_data
        title_frame = bytes([TAG_OUTPUT]) + osc_title
        dead = []
        for client in self.clients:
            try:
                _send_frame(client, output_frame)
                _send_frame(client, title_frame)
            except OSError:
                dead.append(client)
        for client in dead:
            self._remove_client(client)

    def _read_client(self, client: socket.socket) -> None:
        try:
            data = _recv_frame(client)
        except OSError:
            data = None
        if data is None:
            self._remove_client(client)
            return
        self.client_last_active[client] = time.monotonic()
        # Check for winsize frame
        if data and data[0] == TAG_WINSIZE and len(data) == 5:
            rows, cols = struct.unpack("!HH", data[1:5])
            self.client_winsize[client] = (rows, cols)
            # New client becomes active; resize uses min cols from all
            self.active_client = client
            self._set_winsize(*self._effective_winsize(rows, cols))
            return
        # Track active client — resize pty if a different client starts typing
        if client is not self.active_client:
            self.active_client = client
            if client in self.client_winsize:
                rows, cols = self.client_winsize[client]
                self._set_winsize(*self._effective_winsize(rows, cols))
        # Write raw input to pty
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def _effective_winsize(self, rows: int, cols: int) -> tuple[int, int]:
        """Return (rows, min_cols) — use active client's rows but the minimum
        cols across all recently-active clients so every screen renders
        correctly.  Clients idle for more than 60s are excluded."""
        STALE_SECONDS = 60
        now = time.monotonic()
        active_cols = [
            c
            for client, (_, c) in self.client_winsize.items()
            if now - self.client_last_active.get(client, now) < STALE_SECONDS
        ]
        if active_cols:
            cols = min(cols, min(active_cols))
        return rows, cols

    def _set_winsize(self, rows: int, cols: int) -> None:
        self.current_winsize = (rows, cols)
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
        self.client_winsize.pop(client, None)
        self.client_last_active.pop(client, None)
        if self.active_client is client:
            self.active_client = None
        # Min cols may have changed — resize pty for remaining active client
        if self.active_client and self.active_client in self.client_winsize:
            rows, cols = self.client_winsize[self.active_client]
            self._set_winsize(*self._effective_winsize(rows, cols))

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
        if self.cwd_path.exists():
            self.cwd_path.unlink()


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
        try:
            _send_frame(self.sock, _pack_winsize(rows, cols))
        except BrokenPipeError:
            _set_terminal_title("")
            self.sock.close()
            print(f"Session '{self.name}' is dead. Run: sessio kill {self.name}", file=sys.stderr)
            return

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
        # Auto-create session if it doesn't exist
        print(f"session '{name}' not found, creating...")
        cmd_new(name, scrollback_bytes=scrollback_bytes, line_mode=line_mode)
        return
    if line_mode:
        client = LineClient(name, scrollback_bytes=scrollback_bytes)
    else:
        client = RawClient(name, scrollback_bytes=scrollback_bytes)
    client.run()


def cmd_list() -> None:
    print(f"sessio {VERSION}")
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
            cwd_path = SESSIO_DIR / f"{name}.cwd"
            cwd = ""
            try:
                cwd = cwd_path.read_text().strip()
            except (OSError, FileNotFoundError):
                pass
            if not cwd:
                try:
                    child_pid = subprocess.run(
                        ["ps", "--ppid", str(pid), "-o", "pid="],
                        capture_output=True, text=True, timeout=2
                    ).stdout.strip().split()[0]
                    cwd = os.readlink(f"/proc/{child_pid}/cwd")
                except (OSError, IndexError, subprocess.TimeoutExpired):
                    pass
            parts = [f"  {name} (pid {pid})"]
            if cwd:
                parts.append(cwd)
            if title:
                parts.append(f"— {title}")
            print("  ".join(parts) if len(parts) > 1 else parts[0])
        except (ProcessLookupError, ValueError):
            print(f"  {name} (stale)")
            pf.unlink(missing_ok=True)
            sock = SESSIO_DIR / f"{name}.sock"
            sock.unlink(missing_ok=True)
            title = SESSIO_DIR / f"{name}.title"
            title.unlink(missing_ok=True)
            cwd_file = SESSIO_DIR / f"{name}.cwd"
            cwd_file.unlink(missing_ok=True)


def _kill_one(name: str) -> None:
    """Kill a single session by name and clean up its files."""
    pid_path = SESSIO_DIR / f"{name}.pid"
    if not pid_path.exists():
        print(f"error: no session named '{name}'", file=sys.stderr)
        return
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
    cwd_path = SESSIO_DIR / f"{name}.cwd"
    cwd_path.unlink(missing_ok=True)


def cmd_kill(names: list[str]) -> None:
    if not names:
        # No args — offer to kill all
        SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
        pid_files = sorted(SESSIO_DIR.glob("*.pid"))
        all_names = [pf.stem for pf in pid_files]
        if not all_names:
            print("no active sessions")
            return
        print("Active sessions:")
        for n in all_names:
            print(f"  {n}")
        try:
            answer = input(f"Kill all {len(all_names)} sessions? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer.strip().lower() != "y":
            return
        for n in all_names:
            _kill_one(n)
    else:
        for name in names:
            _kill_one(name)


# ── Sandbox ───────────────────────────────────────────────────────────

SANDBOX_CONF = SESSIO_DIR / "sandbox.conf"
SESSIONS_MAP = SESSIO_DIR / "claude-sessions"  # sessio-name → claude UUID

DEFAULT_SANDBOX_CONF = """\
[sandbox]
# Command to run inside sandbox (default: claude --dangerously-skip-permissions)
command = claude --dangerously-skip-permissions

# Paths to bind read-write into sandbox (one per line, blank lines ignored)
# %(home)s expands to $HOME
rw_paths =
    %(home)s/.claude

# Paths to bind read-only into sandbox (missing paths silently skipped)
ro_paths =
    %(home)s/.nvm
    %(home)s/.local/bin
    %(home)s/.local/share/claude
    %(home)s/.gitconfig
    %(home)s/.tmux.conf
    /opt/tools
    /opt/boost

# Extra PATH directories inside sandbox
extra_path =
    %(home)s/.local/bin

# Environment variables to set inside sandbox (KEY=VALUE, one per line)
env =
    CLAUDE_CONFIG_DIR=%(home)s/.claude
"""


def _load_sandbox_conf() -> configparser.ConfigParser:
    """Load sandbox.conf, creating default if missing."""
    conf = configparser.ConfigParser(defaults={"home": str(pathlib.Path.home())})
    if SANDBOX_CONF.exists():
        conf.read(str(SANDBOX_CONF))
    else:
        conf.read_string(DEFAULT_SANDBOX_CONF)
    if not conf.has_section("sandbox"):
        conf.read_string(DEFAULT_SANDBOX_CONF)
    return conf


def _conf_lines(conf: configparser.ConfigParser, key: str) -> list[str]:
    """Parse a multiline config value into a list of non-empty lines."""
    raw = conf.get("sandbox", key, fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _get_claude_uuid(name: str) -> str | None:
    """Look up Claude session UUID for a sessio session name."""
    try:
        for line in SESSIONS_MAP.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == name:
                return parts[1]
    except FileNotFoundError:
        pass
    return None


def _set_claude_uuid(name: str, uuid: str) -> None:
    """Store or update Claude session UUID for a sessio session name."""
    SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
    lines = []
    try:
        lines = SESSIONS_MAP.read_text().splitlines(keepends=True)
    except FileNotFoundError:
        pass
    with open(SESSIONS_MAP, "w") as f:
        replaced = False
        for line in lines:
            parts = line.strip().split()
            if parts and parts[0] == name:
                f.write(f"{name} {uuid}\n")
                replaced = True
            else:
                f.write(line)
        if not replaced:
            f.write(f"{name} {uuid}\n")


def _session_exists(name: str) -> bool:
    """Check if a session is alive."""
    pid_path = SESSIO_DIR / f"{name}.pid"
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def cmd_sandbox(args: list[str]) -> None:
    """Launch claude in a bwrap sandbox inside a sessio session."""
    shell_mode = "--shell" in args
    positional = [a for a in args if not a.startswith("-")]

    name_arg = positional[0] if len(positional) >= 1 else None
    dir_arg = positional[1] if len(positional) >= 2 else None

    # Resolve name and directory
    if name_arg and _session_exists(name_arg):
        # Existing session — just attach
        cmd_attach(name_arg)
        return

    if name_arg and dir_arg:
        name = name_arg
        project_dir = os.path.abspath(dir_arg)
    elif name_arg and os.path.isabs(name_arg) and os.path.isdir(name_arg):
        project_dir = name_arg
        name = os.path.basename(project_dir)
    elif name_arg:
        name = name_arg
        project_dir = os.getcwd()
    else:
        project_dir = os.getcwd()
        name = os.path.basename(project_dir)

    if not shutil.which("bwrap"):
        print("error: bwrap not found. Install: sudo apt install bubblewrap", file=sys.stderr)
        sys.exit(1)

    conf = _load_sandbox_conf()
    command = "bash" if shell_mode else conf.get("sandbox", "command",
                                                  fallback="claude --dangerously-skip-permissions")
    rw_paths = _conf_lines(conf, "rw_paths")
    ro_paths = _conf_lines(conf, "ro_paths")
    extra_path = _conf_lines(conf, "extra_path")
    env_lines = _conf_lines(conf, "env")

    # Check for claude resume UUID (scoped by sessio name)
    claude_project_dir = None
    if not shell_mode and "claude" in command:
        claude_project_key = project_dir.replace("/", "-")
        claude_project_dir = str(pathlib.Path.home() / ".claude" / "projects" / claude_project_key)
        existing_uuid = _get_claude_uuid(name)

        if existing_uuid and os.path.exists(os.path.join(claude_project_dir, f"{existing_uuid}.jsonl")):
            command = f"claude -r {existing_uuid} --dangerously-skip-permissions"
            print(f"Session: {name} (resuming {existing_uuid})")
        else:
            print(f"Session: {name} (new)")
    elif shell_mode:
        print(f"Session: {name} (shell)")

    # Build bwrap wrapper script
    script_path = os.path.abspath(sys.argv[0])
    wrapper_path = SESSIO_DIR / f"{name}.wrapper.sh"
    SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)

    # Build bwrap command line
    home = str(pathlib.Path.home())
    bwrap_args = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--dir", "/tmp",
        "--dir", "/var",
        "--symlink", "../tmp", "var/tmp",
        "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind", "/etc/ssl", "/etc/ssl",
        "--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates",
        "--dir", f"/run/user/{os.getuid()}",
        "--setenv", "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}",
        "--dir", home,
        "--bind", project_dir, project_dir,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--setenv", "HOME", home,
        "--setenv", "PS1", f"[sandbox:{project_dir}] \\w\\$ ",
        "--chdir", project_dir,
    ]

    # RW paths
    for p in rw_paths:
        if os.path.exists(p):
            bwrap_args += ["--bind", p, p]

    # RO paths
    for p in ro_paths:
        if os.path.exists(p):
            bwrap_args += ["--ro-bind", p, p]

    # Also bind /etc/passwd and /etc/group info via process substitution in wrapper
    # Build PATH
    sandbox_path = "/usr/local/bin:/usr/bin:/bin"
    for p in extra_path:
        if os.path.isdir(p):
            sandbox_path = f"{p}:{sandbox_path}"
    bwrap_args += ["--setenv", "PATH", sandbox_path]

    # Extra env
    for ev in env_lines:
        if "=" in ev:
            k, v = ev.split("=", 1)
            bwrap_args += ["--setenv", k, v]

    # Build the shell command that bwrap will exec
    inner_cmd = f'exec {command}'

    # Quote bwrap args for the wrapper script
    quoted_args = " \\\n    ".join(f"'{a}'" for a in bwrap_args)
    passwd_line = subprocess.run(["getent", "passwd", str(os.getuid())],
                                 capture_output=True, text=True).stdout.strip()
    group_line = subprocess.run(["getent", "group", str(os.getgid())],
                                capture_output=True, text=True).stdout.strip()

    wrapper_script = f"""#!/bin/bash
rm -f '{wrapper_path}'
{quoted_args} \\
    --file 11 /etc/passwd \\
    --file 12 /etc/group \\
    bash -c '{inner_cmd}' \\
    11< <(echo '{passwd_line}') \\
    12< <(echo '{group_line}')
STATUS=$?
if [[ $STATUS -ne 0 ]]; then
    echo "bwrap exited with status $STATUS. Press any key to close."
    read -rsn1
fi
exit $STATUS
"""
    wrapper_path.write_text(wrapper_script)
    wrapper_path.chmod(0o700)

    # Launch sessio with wrapper as SHELL
    old_shell = os.environ.get("SHELL")
    os.environ["SHELL"] = str(wrapper_path)
    try:
        cmd_new(name)
    finally:
        if old_shell:
            os.environ["SHELL"] = old_shell
        else:
            os.environ.pop("SHELL", None)

    # After exit, find the most recently modified session file and update mapping
    if claude_project_dir and not shell_mode:
        try:
            all_files = [f for f in os.listdir(claude_project_dir) if f.endswith(".jsonl")]
        except FileNotFoundError:
            all_files = []
        if all_files:
            latest = max(all_files,
                         key=lambda f: os.path.getmtime(os.path.join(claude_project_dir, f)))
            _set_claude_uuid(name, latest.replace(".jsonl", ""))


# ── Menu ──────────────────────────────────────────────────────────────

def cmd_menu(args: list[str]) -> None:
    """Interactive session picker. Use from .bashrc for VPN login."""
    # Parse --vpn IPs
    vpn_ips: list[str] = []
    for i, a in enumerate(args):
        if a == "--vpn" and i + 1 < len(args):
            vpn_ips = [ip.strip() for ip in args[i + 1].split(",")]

    # VPN check (skip menu if not from VPN)
    if vpn_ips:
        client_ip = os.environ.get("SSH_CONNECTION", "").split()[0] if os.environ.get("SSH_CONNECTION") else ""
        if not client_ip or client_ip not in vpn_ips:
            return

    SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
    pid_files = sorted(SESSIO_DIR.glob("*.pid"))

    # Build session list with live PIDs
    sessions: list[tuple[str, int, str]] = []  # (name, pid, cwd)
    for pf in pid_files:
        name = pf.stem
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError, OSError):
            continue
        # Get CWD: try .cwd file first, fall back to /proc
        cwd = ""
        cwd_path = SESSIO_DIR / f"{name}.cwd"
        try:
            cwd = cwd_path.read_text().strip()
        except (OSError, FileNotFoundError):
            pass
        if not cwd:
            try:
                child_pid = subprocess.run(
                    ["ps", "--ppid", str(pid), "-o", "pid="],
                    capture_output=True, text=True, timeout=2
                ).stdout.strip().split()[0]
                cwd = os.readlink(f"/proc/{child_pid}/cwd")
            except (OSError, IndexError, subprocess.TimeoutExpired):
                pass
        sessions.append((name, pid, cwd))

    if not sessions:
        print("No active sessions.")
        return

    # Sort by socket mtime (most recently used first)
    sessions.sort(key=lambda s: os.path.getmtime(str(SESSIO_DIR / f"{s[0]}.sock"))
                  if (SESSIO_DIR / f"{s[0]}.sock").exists() else 0, reverse=True)

    count = len(sessions)
    width = len(str(count))

    print()
    print(f"  \033[1mSessio {VERSION}\033[0m")
    print()
    for i, (name, pid, cwd) in enumerate(sessions):
        cwd_display = f"  \033[2m{cwd}\033[0m" if cwd else ""
        print(f"  {i + 1:>{width}}  {name:<20s}{cwd_display}")
    print(f"  {'q':>{width}}  Exit")
    print()

    if count <= 9:
        sys.stdout.write("  Select: ")
        sys.stdout.flush()
        # Single keypress mode
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        try:
            tty.setraw(sys.stdin.fileno())
            while True:
                ch = sys.stdin.read(1)
                if ch == "q":
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
                    print("q")
                    return
                if ch.isdigit():
                    idx = int(ch) - 1
                    if 0 <= idx < count:
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
                        print(ch)
                        name = sessions[idx][0]
                        os.execlp("sessio", "sessio", "attach", name)
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
    else:
        try:
            choice = input("  Select: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice.strip() == "q":
            return
        try:
            idx = int(choice.strip()) - 1
            if 0 <= idx < count:
                name = sessions[idx][0]
                os.execlp("sessio", "sessio", "attach", name)
        except ValueError:
            pass
        print("Invalid selection.")


# ── Install ───────────────────────────────────────────────────────────

BASHRC_BLOCK = '''
# --- sessio shell integration ---
__sessio_osc7() { printf '\\e]7;file://%s%s\\a' "$HOSTNAME" "$PWD"; }
PROMPT_COMMAND="__sessio_osc7${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
alias cs='sessio sandbox'
if [[ -n "$SSH_CONNECTION" ]] && command -v sessio &>/dev/null; then
    echo "Available sessions:"
    sessio list
    echo '  Type "sessio menu" or "cs <name>" to connect'
fi
[[ -n "$SESSIO_SESSION" ]] && echo "Current sessio: $SESSIO_SESSION"
# --- end sessio ---
'''

ZSHRC_BLOCK = '''
# --- sessio shell integration ---
chpwd() { printf '\\e]7;file://%s%s\\a' "$HOST" "$PWD" }
alias cs='sessio sandbox'
if [[ -n "$SSH_CONNECTION" ]] && (( $+commands[sessio] )); then
    echo "Available sessions:"
    sessio list
    echo '  Type "sessio menu" or "cs <name>" to connect'
fi
[[ -n "$SESSIO_SESSION" ]] && echo "Current sessio: $SESSIO_SESSION"
# --- end sessio ---
'''


def cmd_install() -> None:
    """Install sessio to PATH and configure shell integration."""
    src = pathlib.Path(__file__).resolve()

    # 1. Copy/symlink to PATH
    local_bin = pathlib.Path.home() / ".local" / "bin"
    system_bin = pathlib.Path("/usr/local/bin")

    print("Install sessio binary:")
    print(f"  1  {local_bin}/sessio (user, no sudo)")
    print(f"  2  {system_bin}/sessio (system, needs sudo)")
    print(f"  s  Skip")
    try:
        choice = input("  Select [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if choice == "1":
        local_bin.mkdir(parents=True, exist_ok=True)
        dest = local_bin / "sessio"
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        shutil.copy2(str(src), str(dest))
        dest.chmod(0o755)
        print(f"  Installed {dest}")
        if str(local_bin) not in os.environ.get("PATH", ""):
            print(f"  WARNING: {local_bin} is not in your PATH")
    elif choice == "2":
        dest = system_bin / "sessio"
        try:
            subprocess.run(["sudo", "install", "-m", "755", str(src), str(dest)], check=True)
            print(f"  Installed {dest}")
        except subprocess.CalledProcessError:
            print("  Failed to install (sudo error)", file=sys.stderr)
            return
    else:
        print("  Skipped.")

    # 2. Shell integration
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        rc_path = pathlib.Path.home() / ".zshrc"
        block = ZSHRC_BLOCK
    else:
        rc_path = pathlib.Path.home() / ".bashrc"
        block = BASHRC_BLOCK

    print(f"\nShell integration ({rc_path}):")
    print("  This adds: OSC 7 CWD tracking, 'cs' alias, session list on SSH login")

    # Check if already installed
    try:
        existing = rc_path.read_text()
        if "sessio shell integration" in existing:
            print("  Already installed. Skipped.")
        else:
            try:
                answer = input("  Add to shell rc? [Y/n]: ").strip().lower() or "y"
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if answer == "y":
                with open(rc_path, "a") as f:
                    f.write(block)
                print(f"  Added to {rc_path}")
            else:
                print("  Skipped.")
    except FileNotFoundError:
        try:
            answer = input(f"  Create {rc_path}? [Y/n]: ").strip().lower() or "y"
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer == "y":
            rc_path.write_text(block)
            print(f"  Created {rc_path}")

    # 3. Sandbox config
    print(f"\nSandbox config ({SANDBOX_CONF}):")
    if SANDBOX_CONF.exists():
        print("  Already exists. Skipped.")
    else:
        try:
            answer = input("  Create default config? [Y/n]: ").strip().lower() or "y"
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer == "y":
            SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
            SANDBOX_CONF.write_text(DEFAULT_SANDBOX_CONF)
            print(f"  Created {SANDBOX_CONF}")
            print("  Edit this file to configure bwrap paths for your system.")
        else:
            print("  Skipped.")

    print("\nDone. Restart your shell or run: source " + str(rc_path))


def cmd_uninstall() -> None:
    """Remove sessio from PATH, shell rc, and optionally ~/.sessio."""
    # 1. Remove binary
    removed_bin = False
    local_dest = pathlib.Path.home() / ".local" / "bin" / "sessio"
    system_dest = pathlib.Path("/usr/local/bin/sessio")
    if local_dest.is_symlink() or local_dest.exists():
        try:
            local_dest.unlink()
            print(f"  Removed {local_dest}")
            removed_bin = True
        except OSError as e:
            print(f"  Failed to remove {local_dest}: {e}", file=sys.stderr)
    if system_dest.is_symlink() or system_dest.exists():
        try:
            system_dest.unlink()
            print(f"  Removed {system_dest}")
            removed_bin = True
        except PermissionError:
            print(f"  Skipped {system_dest} (run as root to remove)")
        except OSError as e:
            print(f"  Failed to remove {system_dest}: {e}", file=sys.stderr)
    if not removed_bin:
        print("  No binary found in PATH.")

    # 2. Remove shell integration block from rc files
    for rc_path in [pathlib.Path.home() / ".bashrc", pathlib.Path.home() / ".zshrc"]:
        if not rc_path.exists():
            continue
        try:
            content = rc_path.read_text()
        except OSError:
            continue
        if "--- sessio shell integration ---" not in content:
            continue
        # Remove the block between markers
        lines = content.splitlines(keepends=True)
        new_lines = []
        skipping = False
        for line in lines:
            if "--- sessio shell integration ---" in line:
                skipping = True
                continue
            if skipping and "--- end sessio ---" in line:
                skipping = False
                continue
            if not skipping:
                new_lines.append(line)
        # Strip trailing blank lines left by removal
        new_content = "".join(new_lines).rstrip("\n") + "\n"
        rc_path.write_text(new_content)
        print(f"  Removed shell integration from {rc_path}")

    # 3. Optionally remove ~/.sessio
    if SESSIO_DIR.exists():
        try:
            answer = input(f"\n  Remove {SESSIO_DIR} and all config? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer == "y":
            import shutil as _shutil
            _shutil.rmtree(SESSIO_DIR, ignore_errors=True)
            print(f"  Removed {SESSIO_DIR}")
        else:
            print(f"  Kept {SESSIO_DIR}")

    print("\nDone. Restart your shell to complete uninstall.")


# ── Main ───────────────────────────────────────────────────────────────

USAGE = f"""\
sessio {VERSION} — persistent terminal sessions

usage: sessio <command> [args]

commands:
  new <name> [opts]                 create a new session and attach
  attach <name> [opts]              attach to session (creates if needed)
  list                              list active sessions
  kill [name...]                    kill sessions (no args = kill all)
  sandbox [--shell] [name] [dir]    claude in bwrap sandbox (alias: cs)
  menu [--vpn IPs]                  interactive session picker
  install                           install to PATH and configure shell
  uninstall                         remove from PATH, shell rc, and config

options:
  -s, --scrollback BYTES   scrollback bytes on attach (default: 2048, 0=none, -1=all)
  --line                   use line mode (readline) instead of raw mode
  -h, --help               show this help
  -v, --version            show version

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
        print("usage: sessio <command> [args]  (try 'sessio --help')", file=sys.stderr)
        sys.exit(1)

    cmd = args[0]
    rest = args[1:]

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return

    if cmd in ("-v", "--version", "version"):
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
        cmd_kill(rest)
    elif cmd == "sandbox":
        cmd_sandbox(rest)
    elif cmd == "menu":
        cmd_menu(rest)
    elif cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
