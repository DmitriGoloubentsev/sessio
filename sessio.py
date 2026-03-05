#!/usr/bin/env python3
"""sessio - A lightweight terminal session manager."""

import atexit
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
import threading
import time

SESSIO_DIR = pathlib.Path.home() / ".sessio"
MAX_SCROLLBACK_CHUNKS = 10_000
DEFAULT_SCROLLBACK_BYTES = 2048
HISTORY_FILE = SESSIO_DIR / "history"
HISTORY_LENGTH = 50_000

TAG_OUTPUT = 0x00
TAG_SCROLLBACK = 0x01


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


# ── SessionServer (daemon) ─────────────────────────────────────────────

class SessionServer:
    def __init__(self, name: str):
        self.name = name
        self.sock_path = SESSIO_DIR / f"{name}.sock"
        self.pid_path = SESSIO_DIR / f"{name}.pid"
        self.log_path = SESSIO_DIR / f"{name}.log"
        self.scrollback: list[bytes] = []
        self.clients: list[socket.socket] = []
        self.master_fd: int = -1
        self.proc: subprocess.Popen | None = None
        self.srv_sock: socket.socket | None = None

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
        self.clients.append(conn)

    def _read_pty(self) -> None:
        try:
            data = os.read(self.master_fd, 4096)
        except OSError:
            return
        if not data:
            return
        # Store in scrollback
        self.scrollback.append(data)
        while len(self.scrollback) > MAX_SCROLLBACK_CHUNKS:
            self.scrollback.pop(0)
        # Broadcast to clients
        frame_data = bytes([TAG_OUTPUT]) + data
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
        # Write raw input to pty
        try:
            os.write(self.master_fd, data)
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


# ── SessionClient ──────────────────────────────────────────────────────

class SessionClient:
    def __init__(self, name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES):
        self.name = name
        self.sock_path = SESSIO_DIR / f"{name}.sock"
        self.sock: socket.socket | None = None
        self.stop_event = threading.Event()
        self.scrollback_bytes = scrollback_bytes

    def run(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.sock_path))

        # Receive scrollback
        data = _recv_frame(self.sock)
        if data and len(data) > 1 and data[0] == TAG_SCROLLBACK:
            payload = data[1:]
            if self.scrollback_bytes != 0 and payload:
                # Trim to last scrollback_bytes (-1 means show all)
                if self.scrollback_bytes > 0 and len(payload) > self.scrollback_bytes:
                    payload = payload[-self.scrollback_bytes:]
                print("─── scrollback ───")
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()

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
                    print("\n[detached]")
                    break
                except KeyboardInterrupt:
                    # Send Ctrl-C to the shell
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
        # Parent waits briefly then returns for client to attach
        return

    # First child
    os.setsid()

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Second child — the actual daemon
    # Redirect stdio
    log_fd = os.open(str(server.log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    os.close(log_fd)

    # Ignore SIGHUP
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        server.start()
    except Exception as e:
        sys.stderr.write(f"daemon error: {e}\n")
    finally:
        os._exit(0)


# ── CLI commands ───────────────────────────────────────────────────────

def cmd_new(name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES) -> None:
    SESSIO_DIR.mkdir(mode=0o700, exist_ok=True)
    pid_path = SESSIO_DIR / f"{name}.pid"
    sock_path = SESSIO_DIR / f"{name}.sock"

    if pid_path.exists():
        # Check if process is actually alive
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            print(f"error: session '{name}' already exists (pid {pid})", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Stale pid file, clean up
            pid_path.unlink(missing_ok=True)
            sock_path.unlink(missing_ok=True)

    # Clean up stale socket
    if sock_path.exists() and not pid_path.exists():
        sock_path.unlink()

    server = SessionServer(name)
    daemonize(server)

    # Wait for socket to appear
    for _ in range(20):
        if sock_path.exists():
            break
        time.sleep(0.1)
    else:
        print(f"error: daemon failed to start for '{name}'", file=sys.stderr)
        sys.exit(1)

    cmd_attach(name, scrollback_bytes=scrollback_bytes)


def cmd_attach(name: str, scrollback_bytes: int = DEFAULT_SCROLLBACK_BYTES) -> None:
    sock_path = SESSIO_DIR / f"{name}.sock"
    if not sock_path.exists():
        print(f"error: no session named '{name}'", file=sys.stderr)
        sys.exit(1)
    client = SessionClient(name, scrollback_bytes=scrollback_bytes)
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
            print(f"  {name} (pid {pid})")
        except (ProcessLookupError, ValueError):
            print(f"  {name} (stale)")
            pf.unlink(missing_ok=True)
            sock = SESSIO_DIR / f"{name}.sock"
            sock.unlink(missing_ok=True)


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
    # Clean up
    pid_path.unlink(missing_ok=True)
    sock_path = SESSIO_DIR / f"{name}.sock"
    sock_path.unlink(missing_ok=True)


# ── Main ───────────────────────────────────────────────────────────────

USAGE = """\
usage: sessio <command> [args]

commands:
  new <name> [-s BYTES]      create a new session and attach
  attach <name> [-s BYTES]   attach to an existing session
  list                       list active sessions
  kill <name>                kill a session

options:
  -s, --scrollback BYTES   scrollback bytes to display on attach (default: 2048, 0=none, -1=all)"""


def _parse_scrollback(args: list[str]) -> int:
    for i, a in enumerate(args):
        if a in ("-s", "--scrollback") and i + 1 < len(args):
            val = int(args[i + 1])
            return val if val >= 0 else -1
    return DEFAULT_SCROLLBACK_BYTES


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        sys.exit(1)

    cmd = args[0]
    rest = args[1:]

    if cmd == "new":
        if not rest or rest[0].startswith("-"):
            print("usage: sessio new <name> [-s BYTES]", file=sys.stderr)
            sys.exit(1)
        sb = _parse_scrollback(rest[1:])
        cmd_new(rest[0], scrollback_bytes=sb)
    elif cmd == "attach":
        if not rest or rest[0].startswith("-"):
            print("usage: sessio attach <name> [-s BYTES]", file=sys.stderr)
            sys.exit(1)
        sb = _parse_scrollback(rest[1:])
        cmd_attach(rest[0], scrollback_bytes=sb)
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
