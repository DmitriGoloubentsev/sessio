#!/usr/bin/env bash
# claude-sandbox2.sh - Run Claude Code sandboxed with bubblewrap inside sessio
#
# RW:  project dir, claude config
# RO:  system, node, python/venv, MCP tools, /opt/tools, /opt/boost
# Isolated: /home (except above), /tmp, /var
#
# Usage: cs2 [--shell] [session-name] [project-dir]

set -euo pipefail

# Internal: run bwrap directly (called from sessio)
if [[ "${1:-}" == "__bwrap__" ]]; then
    set +euo pipefail
    shift
    CLAUDE_CMD="$1"; shift
    PROJECT_DIR="$1"; shift
    HOME_DIR="$HOME"
    CLAUDE_CONFIG="$HOME/.claude"
    CLAUDE_INSTALL="$HOME/.local/share/claude"
    CLAUDE_BIN="$HOME/.local/bin"
    NVM_DIR="$HOME/.nvm"
    VENV_DIR="$HOME/venv/dev"
    CLANGD_MCP="/opt/dimach/Build/clangd-mcp-server"
    ANDROID_SDK="/opt/dimach/Android/Sdk"
    ANDROID_JBR="/opt/android-studio/jbr"

    BWRAP_ARGS=(
        --ro-bind /usr /usr
        --symlink usr/lib /lib
        --symlink usr/lib64 /lib64
        --symlink usr/bin /bin
        --symlink usr/sbin /sbin
        --proc /proc
        --dev /dev
        --dir /tmp
        --dir /var
        --symlink ../tmp var/tmp

        --ro-bind /etc/resolv.conf /etc/resolv.conf
        --ro-bind /etc/ssl /etc/ssl
        --ro-bind /etc/ca-certificates /etc/ca-certificates

        --dir "/run/user/$(id -u)"
        --setenv XDG_RUNTIME_DIR "/run/user/$(id -u)"

        --dir "$HOME_DIR"
        --bind "$PROJECT_DIR" "$PROJECT_DIR"

        --unshare-all
        --share-net
        --die-with-parent

        --unsetenv CLAUDECODE
        --setenv HOME "$HOME_DIR"
        --setenv PS1 "[sandbox:$PROJECT_DIR] \w\$ "

        --file 11 /etc/passwd
        --file 12 /etc/group
        --chdir "$PROJECT_DIR"
    )

    # Optional ro-binds (skip if source path missing)
    for p in "$CLAUDE_CONFIG" "$CLAUDE_INSTALL" "$CLAUDE_BIN" \
             "$NVM_DIR" "$VENV_DIR" "$CLANGD_MCP" "$ANDROID_SDK" "$ANDROID_JBR" \
             /etc/java-17-openjdk \
             ~/.tmux.conf ~/.gitconfig /home/dimach/miniconda3/bin /opt/tools /opt/boost; do
        [[ -e "$p" ]] || continue
        if [[ "$p" == "$CLAUDE_CONFIG" ]]; then
            BWRAP_ARGS+=(--bind "$p" "$p")
        else
            BWRAP_ARGS+=(--ro-bind "$p" "$p")
        fi
    done

    # Build PATH from dirs that exist
    SANDBOX_PATH="/usr/local/bin:/usr/bin:/bin"
    [[ -d "$CLAUDE_BIN" ]] && SANDBOX_PATH="$CLAUDE_BIN:$SANDBOX_PATH"
    [[ -d "$VENV_DIR" ]] && SANDBOX_PATH="$VENV_DIR/bin:$SANDBOX_PATH"
    [[ -d "$NVM_DIR" ]] && SANDBOX_PATH="$NVM_DIR/versions/node/v20.19.6/bin:$SANDBOX_PATH"
    [[ -d "$ANDROID_JBR" ]] && SANDBOX_PATH="$ANDROID_JBR/bin:$SANDBOX_PATH"
    [[ -d "$ANDROID_SDK" ]] && SANDBOX_PATH="$ANDROID_SDK/platform-tools:$ANDROID_SDK/cmdline-tools/latest/bin:$ANDROID_SDK/build-tools/$(ls "$ANDROID_SDK/build-tools" 2>/dev/null | sort -V | tail -1):$SANDBOX_PATH"
    BWRAP_ARGS+=(--setenv PATH "$SANDBOX_PATH")
    BWRAP_ARGS+=(--setenv CLAUDE_CONFIG_DIR "$CLAUDE_CONFIG")
    [[ -d "$NVM_DIR" ]] && BWRAP_ARGS+=(--setenv NODE_PATH "$NVM_DIR/versions/node/v20.19.6/lib/node_modules")
    [[ -d "$VENV_DIR" ]] && BWRAP_ARGS+=(--setenv VIRTUAL_ENV "$VENV_DIR")
    [[ -d "$ANDROID_SDK" ]] && BWRAP_ARGS+=(--setenv ANDROID_HOME "$ANDROID_SDK" --setenv ANDROID_SDK_ROOT "$ANDROID_SDK")
    if [[ -d "$ANDROID_JBR" ]]; then
        BWRAP_ARGS+=(--setenv JAVA_HOME "$ANDROID_JBR")
    elif [[ -d /usr/lib/jvm/default-java ]]; then
        BWRAP_ARGS+=(--setenv JAVA_HOME /usr/lib/jvm/default-java)
    fi

    # Build inner command
    INNER_CMD=""
    [[ -d "$VENV_DIR" ]] && INNER_CMD='source "$VIRTUAL_ENV/bin/activate" && '
    INNER_CMD+='export PS1="[sandbox:'"$PROJECT_DIR"'] \w\$ " && exec '"$CLAUDE_CMD"

    bwrap "${BWRAP_ARGS[@]}" \
        bash -c "$INNER_CMD" \
        11< <(getent passwd $UID 65534) \
        12< <(getent group $(id -g) 65534)
    STATUS=$?
    if [[ $STATUS -ne 0 ]]; then
        echo "bwrap exited with status $STATUS. Press any key to close."
        read -rsn1
    fi
    exit $STATUS
fi

# --- Main entry point ---

random_word() {
    local words=/usr/share/dict/american-english
    local count
    count=$(wc -l < "$words")
    while true; do
        local line=$(( RANDOM % count + 1 ))
        local word
        word=$(sed -n "${line}p" "$words")
        if [[ "$word" =~ ^[a-z]+$ ]]; then
            echo "$word"
            return
        fi
    done
}

SHELL_MODE=false
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --shell) SHELL_MODE=true ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done

SESSION_NAME="${POSITIONAL[0]:-$(basename "$(pwd)")}"
PROJECT_DIR="${POSITIONAL[1]:-$(pwd)}"

# If sessio session already exists, just attach
SESSIO_PID_FILE="$HOME/.sessio/${SESSION_NAME}.pid"
if [[ -f "$SESSIO_PID_FILE" ]] && kill -0 "$(cat "$SESSIO_PID_FILE" 2>/dev/null)" 2>/dev/null; then
    exec /usr/local/bin/sessio attach "$SESSION_NAME"
fi

if $SHELL_MODE; then
    CLAUDE_CMD="bash"
    echo "Session: $SESSION_NAME (shell)"
else
    SESSIONS_MAP="$HOME/.claude/sandbox-sessions"
    CLAUDE_PROJECT_PATH=$(echo "$PROJECT_DIR" | tr '/' '-')
    SESSION_DIR="$HOME/.claude/projects/${CLAUDE_PROJECT_PATH}"

    # Look up existing session mapping
    EXISTING_UUID=$(awk -v name="$SESSION_NAME" '$1 == name {print $2}' "$SESSIONS_MAP" 2>/dev/null || true)

    if [[ -n "$EXISTING_UUID" && -f "$SESSION_DIR/${EXISTING_UUID}.jsonl" ]]; then
        CLAUDE_CMD="claude -r $EXISTING_UUID --dangerously-skip-permissions"
        echo "Session: $SESSION_NAME (resuming $EXISTING_UUID)"
    else
        CLAUDE_CMD="claude --dangerously-skip-permissions"
        echo "Session: $SESSION_NAME (new)"
    fi
fi

SCRIPT_PATH="$(readlink -f "$0")"

# Snapshot session files before launching
if ! $SHELL_MODE; then
    BEFORE=$(ls "$SESSION_DIR" 2>/dev/null | sort || true)
fi

# Create wrapper script for sessio (it spawns $SHELL with no args)
WRAPPER="$HOME/.sessio/${SESSION_NAME}.wrapper.sh"
mkdir -p "$HOME/.sessio"
CLAUDE_CMD_Q=$(printf '%q' "$CLAUDE_CMD")
PROJECT_DIR_Q=$(printf '%q' "$PROJECT_DIR")
cat > "$WRAPPER" << WRAPPER_EOF
#!/bin/bash
rm -f "$WRAPPER"
exec "$SCRIPT_PATH" __bwrap__ $CLAUDE_CMD_Q $PROJECT_DIR_Q
WRAPPER_EOF
chmod +x "$WRAPPER"

# Launch sessio with wrapper as SHELL
SHELL="$WRAPPER" /usr/local/bin/sessio new "$SESSION_NAME"

# After claude exits, record session name → uuid mapping
if ! $SHELL_MODE; then
    AFTER=$(ls "$SESSION_DIR" 2>/dev/null | sort || true)
    NEW_FILE=$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | head -1)

    if [[ -n "$NEW_FILE" ]]; then
        NEW_UUID="${NEW_FILE%.jsonl}"
        if ! awk -v name="$SESSION_NAME" '$1 == name {found=1} END {exit !found}' "$SESSIONS_MAP" 2>/dev/null; then
            echo "$SESSION_NAME $NEW_UUID" >> "$SESSIONS_MAP"
        fi
    fi
fi
