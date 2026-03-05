**mux.py**

Specification & Implementation Plan

*A lightweight, mobile-friendly terminal session manager built in
Python*

**1. Overview**

mux.py is a pure-Python terminal session manager designed as a simpler,
more mobile-stable alternative to tmux. It provides persistent shell
sessions that survive terminal closures, with readline-based input for
clean and predictable keyboard behaviour on both desktop and mobile
devices (including Termux on Android).

Unlike tmux, mux.py makes no attempt to multiplex a single terminal into
multiple panes or provide a full-screen UI. Its value proposition is
reliability and simplicity: one shell per named session, clean
line-based I/O, and full scrollback replay on re-attach.

**2. Goals & Non-Goals**

**Goals**

-   Session persistence --- shell sessions survive closing the client
    terminal

-   Scrollback replay --- full output history shown on re-attach

-   Readline I/O --- arrow-key history, Ctrl+R search, tab completion
    always work

-   Mobile stability --- no escape-code artefacts on Android/Termux
    keyboards

-   Zero dependencies --- stdlib only, works wherever Python 3.10+ is
    installed

-   Simple CLI --- four commands: new, attach, list, kill

**Non-Goals**

-   Split panes or window management

-   Full-screen TUI / ncurses rendering

-   Running interactive programs inside the session (vim, htop, etc.)

-   Network (TCP) transport --- Unix sockets only

**3. Architecture**

The system is split into two roles that communicate over a Unix domain
socket:

  ------------------ ----------------------------------------------------
  **Component**      **Responsibility**

  SessionServer      Daemon process: owns the pty, holds the shell
                     subprocess, stores scrollback ring-buffer, accepts
                     client connections, broadcasts shell output, relays
                     client input to the pty

  SessionClient      Foreground process: connects to the daemon socket,
                     replays scrollback on attach, reads user input via
                     readline, forwards input frames to server, prints
                     output to stdout
  ------------------ ----------------------------------------------------

**Process Model**

When the user runs mux new \<name\>, the script double-forks to detach a
daemon. The daemon writes its PID to \~/.mux/\<name\>.pid and creates a
Unix socket at \~/.mux/\<name\>.sock. The original process then becomes
a client and immediately attaches.

When the user runs mux attach \<name\>, a client process connects to the
existing socket. Multiple clients can attach to the same session
simultaneously; all receive broadcast output.

**Wire Protocol**

All messages are length-prefixed frames:

> \[ 4 bytes big-endian uint32 length \]\[ payload bytes \]

The first byte of each payload is a type tag:

  --------- -------------------------------------------------------------
  **Tag**   **Meaning**

  0x00      Shell output --- broadcast from server to all clients

  0x01      Scrollback dump --- sent once to a newly connected client

  (none)    Client input --- raw bytes forwarded directly to the pty
            master fd
  --------- -------------------------------------------------------------

**4. Data Flow**

**Attach sequence**

1.  Client connects to Unix socket

2.  Server sends 0x01 frame containing concatenated scrollback buffer

3.  Client prints scrollback, then enters interactive readline loop

4.  Server streams subsequent 0x00 frames as the shell produces output

**Input sequence**

5.  readline delivers a completed line to the client

6.  Client wraps it in a length-prefixed frame and sends to server

7.  Server writes raw bytes to pty master fd

8.  Shell processes the input; output flows back via the pty and is
    broadcast

**5. State & File Layout**

  ----------------------- ----------------------------------------------------
  **Path**                **Purpose**

  \~/.mux/                Top-level state directory, created on first run

  \~/.mux/\<name\>.sock   Unix domain socket for the session

  \~/.mux/\<name\>.pid    PID of the daemon process

  \~/.mux/\<name\>.log    Daemon stdout/stderr (for debugging)

  \~/.mux/history         Persistent readline history file (shared across all
                          sessions)
  ----------------------- ----------------------------------------------------

**6. CLI Reference**

  ------------------ ----------------------------------------------------
  **Command**        **Behaviour**

  mux new \<name\>   Spawn daemon for a new session named \<name\>, then
                     attach. Exits with error if session already exists.

  mux attach         Connect to an existing session. Replays scrollback
  \<name\>           then goes interactive.

  mux list           Print all active sessions (reads \*.pid files in
                     \~/.mux/).

  mux kill \<name\>  Send SIGTERM to the daemon for \<name\> and clean up
                     state files.
  ------------------ ----------------------------------------------------

**7. Implementation Plan**

The implementation is broken into five phases. Each phase is
independently testable.

**Phase 1 --- Core pty daemon**

  ------------------ ----------------------------------------------------
  **Task**           **Detail**

  1.1 Pty spawn      Use pty.openpty() + subprocess.Popen to launch
                     \$SHELL with slave_fd for all stdio. Store master_fd
                     on the server object.

  1.2 Unix socket    Bind AF_UNIX SOCK_STREAM to \~/.mux/\<name\>.sock.
                     Set non-blocking. Accept connections in select()
                     loop.

  1.3 select() loop  Monitor \[srv_socket, master_fd, \*clients\]. Read
                     from master_fd → scrollback list → broadcast. Read
                     from clients → write to master_fd.

  1.4 Double-fork    Implement daemonize() with double-fork, setsid(),
                     redirect stdin/stdout/stderr, write PID file.

  1.5 Cleanup        On shell exit (proc.poll() != None), unlink .pid and
                     .sock, close server socket.
  ------------------ ----------------------------------------------------

**Phase 2 --- Wire protocol**

  ------------------ ----------------------------------------------------
  **Task**           **Detail**

  2.1 Framing        Implement \_send_frame(sock, data) and
                     \_recv_frame(sock) using 4-byte big-endian length
                     prefix. \_recv_exact() for reliable reads.

  2.2 Type tags      Prefix server→client frames: 0x01 for scrollback
                     dump on new connection, 0x00 for live output.
                     Client→server frames have no tag (raw input bytes).

  2.3 Scrollback     On new client connect, concatenate scrollback list,
  dump               wrap in 0x01 frame, send before adding to broadcast
                     list.
  ------------------ ----------------------------------------------------

**Phase 3 --- Client & readline**

  ------------------ ----------------------------------------------------
  **Task**           **Detail**

  3.1                readline.read_history_file on load, atexit handler
  setup_history()    to write on exit. Set history length to 50 000.

  3.2 Reader thread  Background daemon thread calls \_recv_frame() in a
                     loop. On 0x00 frame, decode and write to stdout with
                     \\r prefix for clean line handling. On None, set
                     stop event.

  3.3 Input loop     Main thread calls input() (readline). EOFError →
                     detach. KeyboardInterrupt → send 0x03 (Ctrl-C) raw
                     frame without going through readline.

  3.4 Scrollback     On connect, if 0x01 payload is non-empty, print
  display            header line, content, and footer line before
                     entering interactive mode.
  ------------------ ----------------------------------------------------

**Phase 4 --- CLI & lifecycle**

  ------------------ ----------------------------------------------------
  **Task**           **Detail**

  4.1 cmd_new        Check for existing .pid file. If present, error.
                     Else instantiate SessionServer, call daemonize(),
                     sleep 0.5 s, then SessionClient.run().

  4.2 cmd_attach     Instantiate SessionClient, call run(). Error if
                     .sock not present.

  4.3 cmd_list       Glob \~/.mux/\*.pid, print name + PID for each.

  4.4 cmd_kill       Read PID from .pid file, os.kill(pid, SIGTERM).

  4.5 main()         Parse sys.argv\[1:\], dispatch to cmd\_\* functions.
                     Print usage on bad input.
  ------------------ ----------------------------------------------------

**Phase 5 --- Robustness & polish**

  ------------------ ----------------------------------------------------
  **Task**           **Detail**

  5.1 Dead client    In broadcast loop, catch OSError, collect dead
  cleanup            clients, remove after iteration.

  5.2 Stale socket   In cmd_new, if .sock exists but .pid does not (crash
                     remnant), unlink stale socket before starting.

  5.3 Scrollback cap Keep scrollback as list of bytes chunks. Pop from
                     front when len \> limit to bound memory.

  5.4 Ctrl-C         Send b\'\\x03\' directly (bypassing frame protocol)
  pass-through       for immediate signal delivery to shell.

  5.5 Tab completion readline.parse_and_bind(\'tab: complete\') in
                     setup_history() for shell-side tab complete via
                     readline.
  ------------------ ----------------------------------------------------

**8. Known Limitations & Future Work**

  ---------------------- ---------------------- --------------------------
  **Limitation**         **Impact**             **Potential fix**

  Line-mode I/O only     Interactive TUI        Add a \--raw flag that
                         programs (vim, htop,   switches the client to raw
                         less) will not render  pty mode
                         correctly              

  Shared readline        All sessions share one Use per-session history
  history                history file; commands files at
                         from session A appear  \~/.mux/\<name\>.history
                         in session B           

  No auth on socket      Any local user can     Set socket permissions to
                         attach to any session  0600; owned by creating
                         if they know the       user
                         socket path            

  Output race on attach  Scrollback replay and  Pause broadcast during
                         live output may        dump, or use a queue
                         interleave if shell is 
                         very active at attach  
                         time                   

  No resize propagation  Terminal window resize Catch SIGWINCH in client,
                         (SIGWINCH) is not      send terminal size frame
                         forwarded to the shell to server, call ioctl
                         pty                    TIOCSWINSZ on master_fd
  ---------------------- ---------------------- --------------------------

**9. Comparison with tmux**

  ---------------------- ---------------------- -------------------------
  **Feature**            **tmux**               **mux.py**

  Session persistence    Yes                    Yes

  Scrollback on attach   Yes (configurable)     Yes (50 000 lines)

  Split panes            Yes                    No

  Mobile keyboard        Fragile (raw mode)     Stable (readline)
  stability                                     

  Readline history       Shell-dependent        Always works
  search                                        

  Interactive programs   Yes (full pty)         No (line mode only)

  Dependencies           C binary, must install Python 3.10+ stdlib only

  Config file            \~/.tmux.conf          None needed
  ---------------------- ---------------------- -------------------------

**10. Glossary**

  ------------------ ----------------------------------------------------
  **Term**           **Definition**

  pty                Pseudo-terminal: a kernel-level pair (master/slave)
                     that emulates a hardware serial terminal. The shell
                     writes to slave; the server reads from master.

  readline           GNU Readline: the library that provides interactive
                     line editing, history, and tab completion for
                     Python\'s input() function.

  Unix socket        An AF_UNIX SOCK_STREAM socket: IPC mechanism using a
                     filesystem path instead of a network address. Faster
                     and simpler than TCP for local communication.

  Scrollback buffer  In-memory list of raw bytes chunks received from the
                     shell pty. Replayed to clients on attach.

  Frame              Length-prefixed message unit used on the wire
                     between client and server.

  Daemon             A background process detached from any controlling
                     terminal, started via double-fork.

  Cooked mode        Terminal input mode where the OS line-disciplines
                     process keystrokes before handing them to the
                     application (enables backspace, Ctrl-C, etc.).
                     readline operates in cooked mode.

  Raw mode           Terminal input mode where every keypress is sent
                     immediately to the application without processing.
                     tmux uses this to forward all input to the shell.
  ------------------ ----------------------------------------------------

*End of document*