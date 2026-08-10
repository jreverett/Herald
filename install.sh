#!/bin/bash
# herald installer for WSL. One command does everything, including Tailscale:
#   curl -fsSL https://raw.githubusercontent.com/jreverett/herald/master/install.sh | bash -s -- --me alice
# Or from a clone: ./install.sh --me alice
set -e
ME=""; PORT=8765; DIR="$HOME/herald"; SKIP_NETWORK=""; AUTH_KEY=""
PEER_NAME=""; PEER_URL=""; PEER_TOKEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --me) ME="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --auth-key) AUTH_KEY="$2"; shift 2 ;;
    --peer) PEER_NAME="$2"; shift 2 ;;
    --peer-url) PEER_URL="$2"; shift 2 ;;
    --peer-token) PEER_TOKEN="$2"; shift 2 ;;
    --skip-network) SKIP_NETWORK=1; shift ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done
[ -n "$ME" ] || { echo "Usage: install.sh --me <name> [--auth-key tskey-...] [--peer <name> --peer-url <url> --peer-token <token>] [--port 8765] [--dir <clone-dir>] [--skip-network]"; exit 1; }

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

banner() {
  echo
  echo "  +--------------------------------------------------------------------+"
  printf '  | %-66s |\n' "$@"
  echo "  +--------------------------------------------------------------------+"
}

TTY=""; [ -t 1 ] && TTY=1
STEP_LOG="${TMPDIR:-/tmp}/herald-install-step.log"

# spin "label" cmd...  - animated spinner on a TTY, plain line otherwise
spin() {
  local label="$1"; shift
  if [ -z "$TTY" ]; then
    echo "  $label..."
    "$@"
    return
  fi
  "$@" >"$STEP_LOG" 2>&1 &
  local pid=$! frames='|/-\' i=0
  while kill -0 "$pid" 2>/dev/null; do
    i=$(( (i+1) % 4 ))
    printf '\r  [%s] %s ' "${frames:$i:1}" "$label"
    sleep 0.15
  done
  local rc=0; wait "$pid" || rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '\r  [ok] %s\n' "$label"
  else
    printf '\r  [!!] %s failed:\n' "$label"
    cat "$STEP_LOG"
  fi
  return "$rc"
}

banner "herald setup - agent-to-agent messaging" \
       "" \
       "This installer will:" \
       "  1. Install Tailscale (private mesh VPN) and join your tailnet" \
       "  2. Write ~/.herald/config.json and put 'herald' on your PATH" \
       "  3. Teach your agents the herald protocol (skill / instructions)" \
       "  4. Start the herald receiver daemon" \
       "  5. (On Windows) Add a system-tray status icon" \
       "" \
       "Security - nothing is exposed outside your private network:" \
       "  - The daemon binds ONLY to the Tailscale interface. No port is" \
       "    opened on your LAN, office network, or the internet." \
       "  - All traffic is WireGuard-encrypted, device-to-device." \
       "  - Senders must present your inbox token; strangers are rejected." \
       "  - Incoming tasks are never auto-executed by your agents."

# 0. locate or fetch the repo (supports curl | bash)
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -f "$SELF_DIR/herald.py" ]; then
  REPO_DIR="$SELF_DIR"
elif [ -f "$DIR/herald.py" ]; then
  REPO_DIR="$DIR"
else
  spin "Fetching herald" git clone -q https://github.com/jreverett/herald.git "$DIR"
  REPO_DIR="$DIR"
fi

# 0.7 migrate a legacy `a2a` install (pre-rename): copy config aside, retire the old daemon.
LEGACY_DIR="$HOME/.a2a"; MIGRATED=""
if [ -d "$LEGACY_DIR" ] && [ ! -d "$HOME/.herald" ]; then
  cp -a "$LEGACY_DIR" "$HOME/.herald"
  MIGRATED=1
  echo "Migrated existing config ~/.a2a -> ~/.herald (token and peers preserved)"
fi
if command -v systemctl >/dev/null && systemctl --user cat a2a-daemon.service >/dev/null 2>&1; then
  systemctl --user disable --now a2a-daemon.service 2>/dev/null || true
fi
pkill -f '[a]2a.py daemon' 2>/dev/null || true

# 0.5 network: Tailscale inside WSL (skippable)
if [ -z "$SKIP_NETWORK" ]; then
  if ! command -v tailscale >/dev/null; then
    banner "PROMPT COMING UP: your sudo password" \
           "" \
           "Why: installing Tailscale, the private VPN herald runs over." \
           "It creates an encrypted device-to-device network; herald will only" \
           "ever listen inside it, so no port is opened to your LAN or the" \
           "internet."
    sudo -v
    spin "Installing Tailscale" sh -c 'curl -fsSL https://tailscale.com/install.sh | sh'
  fi
  if ! pgrep -x tailscaled >/dev/null; then
    if command -v systemctl >/dev/null && systemctl is-system-running >/dev/null 2>&1; then
      sudo systemctl enable --now tailscaled
    else
      echo "Starting tailscaled (no systemd)..."
      sudo nohup tailscaled >/var/tmp/tailscaled.log 2>&1 &
      sleep 2
    fi
  fi
  if [ -z "$(tailscale ip -4 2>/dev/null)" ]; then
    if [ -n "$AUTH_KEY" ]; then
      banner "Joining the shared private network" \
             "" \
             "Using the auth key you were given - no account or sign-up" \
             "needed. Your machine joins your peer's tailnet so their herald" \
             "daemon can reach yours; traffic stays inside the encrypted" \
             "mesh."
      sudo -v
      spin "Joining the private network" sudo tailscale up --auth-key "$AUTH_KEY"
    else
      banner "PROMPT COMING UP: Tailscale login link" \
             "" \
             "Why: this authenticates your machine into the shared tailnet so" \
             "your peer's machine can reach your herald inbox (and only that -" \
             "traffic stays inside the encrypted mesh). Open the printed link" \
             "in your browser and sign in." \
             "" \
             "(No account? Ask your peer for an auth key and rerun with" \
             " --auth-key tskey-... to skip sign-in entirely.)"
      sudo tailscale up
    fi
  fi
fi

# 1. one shared config + inbox for every agent product under this OS user
if [ -f "$HOME/.herald/config.json" ]; then
  echo "~/.herald/config.json already exists, keeping it"
else
  python3 "$REPO_DIR/herald.py" init --me "$ME" --port "$PORT"
fi

# 2. herald on PATH
mkdir -p "$HOME/.local/bin"
printf '#!/bin/bash\nexec python3 "%s/herald.py" "$@"\n' "$REPO_DIR" > "$HOME/.local/bin/herald"
chmod +x "$HOME/.local/bin/herald"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  echo "Added ~/.local/bin to PATH in ~/.bashrc (open a new shell)" ;;
esac

# 3. agent skill/instructions - install into every supported agent profile
# that exists, plus any directory that already has herald or the old a2a skill.
skill_dirs=""
for agent_home in "$HOME/.agents" "$HOME/.claude" "$HOME"/.claude-* "$HOME"/.claude_* \
                  "$HOME/.codex" "$HOME"/.codex-* "$HOME"/.codex_* \
                  "$HOME/.copilot" "$HOME"/.copilot-* "$HOME"/.copilot_*; do
  [ -d "$agent_home" ] && skill_dirs="$skill_dirs $agent_home/skills"
done
[ -n "${CLAUDE_CONFIG_DIR:-}" ] && skill_dirs="$skill_dirs ${CLAUDE_CONFIG_DIR%/}/skills"
[ -n "${CODEX_HOME:-}" ] && skill_dirs="$skill_dirs ${CODEX_HOME%/}/skills"
for d in "$HOME"/.*/skills; do
  if [ -e "$d/herald" ] || [ -e "$d/a2a" ]; then
    skill_dirs="$skill_dirs $d"
  fi
done
installed=""
for d in $skill_dirs; do
  case " $installed " in *" $d "*) continue ;; esac
  installed="$installed $d"
  mkdir -p "$d"
  if [ -L "$d/a2a" ]; then rm -f "$d/a2a"; fi   # retire the pre-rename link
  ln -sfn "$REPO_DIR/skill" "$d/herald"
  echo "Skill installed: $d/herald"
done
for f in "$HOME/.codex/AGENTS.md" "$HOME/.copilot/copilot-instructions.md"; do
  if [ -f "$f" ] && ! grep -q "herald agent protocol pointer" "$f"; then
    printf '\n# herald agent protocol pointer\nFor messaging other people'"'"'s agents (herald), follow %s/skill/SKILL.md\n' "$REPO_DIR" >> "$f"
    echo "Added herald pointer to $f"
  fi
done

# 4. daemon as a systemd user service (falls back to instructions if no systemd)
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/herald-daemon.service" <<EOF
[Unit]
Description=herald receiver daemon
[Service]
ExecStart=/usr/bin/python3 $REPO_DIR/herald.py daemon
Restart=on-failure
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  if systemctl --user enable herald-daemon.service 2>/dev/null \
      && systemctl --user restart herald-daemon.service 2>/dev/null; then
    echo "Daemon running (systemd user service herald-daemon)"
  else
    echo "Could not start the systemd service; start the daemon manually:"
    echo "  nohup herald daemon >~/.herald/daemon.log 2>&1 &"
  fi
else
  echo "No systemd; start the daemon manually: nohup herald daemon >~/.herald/daemon.log 2>&1 &"
fi

# 4.5 Windows tray indicator (only on WSL-with-Windows; skipped cleanly elsewhere)
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && command -v powershell.exe >/dev/null 2>&1; then
  TRAY_SETUP_WIN=$(wslpath -w "$REPO_DIR/tray/setup-tray.ps1" 2>/dev/null || true)
  if [ -n "$TRAY_SETUP_WIN" ]; then
    spin "Adding Windows tray indicator" \
      powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$TRAY_SETUP_WIN" enable || true
    echo "  Tray icon starts at login (and now, if a desktop is available)."
  fi
fi

# 5. connect to a peer and introduce myself (their side runs `herald accept`)
if [ -n "$PEER_NAME" ]; then
  if [ -n "$PEER_URL" ] && [ -n "$PEER_TOKEN" ]; then
    python3 "$REPO_DIR/herald.py" peer add "$PEER_NAME" "$PEER_URL" "$PEER_TOKEN"
    if spin "Connecting to $PEER_NAME" python3 "$REPO_DIR/herald.py" introduce "$PEER_NAME"; then
      banner "Introduced yourself to $PEER_NAME" \
             "" \
             "  you ---------------> $PEER_NAME     delivered" \
             "  you <--------------- $PEER_NAME     once they run: herald accept" \
             "" \
             "Their agent will accept and confirm; then you're connected" \
             "both ways."
    fi
  else
    echo "--peer needs --peer-url and --peer-token too; skipping connect"
  fi
fi

# 4.7 retire legacy `a2a` artefacts, but only once the new daemon is confirmed up (reversible until then).
if "$HOME/.local/bin/herald" status >/dev/null 2>&1; then
  rm -f "$HOME/.local/bin/a2a"
  [ -L "$HOME/.claude/skills/a2a" ] && rm -f "$HOME/.claude/skills/a2a"
  if [ -f "$HOME/.config/systemd/user/a2a-daemon.service" ]; then
    rm -f "$HOME/.config/systemd/user/a2a-daemon.service"
    command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
  fi
  if [ -n "$MIGRATED" ]; then
    rm -rf "$LEGACY_DIR"
    echo "Removed legacy ~/.a2a after verifying herald is up"
  fi
fi

IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
echo
echo "Done. Your address: http://${IP:-<your-tailnet-ip>}:$PORT"
echo "To let someone reach you:  herald peer issue <name>  (send them the two commands it prints)"
echo "To reach someone who issued you a token:"
echo "  herald peer add <name> <their-address> <token> && herald introduce <name>"
