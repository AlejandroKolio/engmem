#!/usr/bin/env bash
# Installs engmem and wires it into OpenAI Codex (CLI + MCP) and ChatGPT (MCP over Secure MCP Tunnel).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/setup-openai.sh [options]

  --store PATH          engmem store (default: $ENGMEM_HOME, else ~/Developer/engmem)
  --no-codex-install    do not install the Codex CLI when it is missing
  --no-sandbox-root     do not add the store to Codex's [sandbox_workspace_write] writable_roots
  --skip-codex          skip the Codex wiring
  --skip-chatgpt        skip the ChatGPT wiring
  -h, --help            show this help

ChatGPT: when `tunnel-client` is on PATH and TUNNEL_ID and CONTROL_PLANE_API_KEY are set,
the tunnel profile `engmem` is initialised too; otherwise the steps are printed.
EOF
}

STORE="${ENGMEM_HOME:-$HOME/Developer/engmem}"
INSTALL_CODEX=1
SANDBOX_ROOT=1
DO_CODEX=1
DO_CHATGPT=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --store) STORE="${2:?--store needs a path}"; shift 2 ;;
    --no-codex-install) INSTALL_CODEX=0; shift ;;
    --no-sandbox-root) SANDBOX_ROOT=0; shift ;;
    --skip-codex) DO_CODEX=0; shift ;;
    --skip-chatgpt) DO_CHATGPT=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STORE="$(mkdir -p "$STORE" && cd "$STORE" && pwd)"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_CONFIG="$CODEX_DIR/config.toml"

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

step "Installing the engmem CLI from $REPO_ROOT"
if command -v uv >/dev/null 2>&1; then
  uv tool install --editable "$REPO_ROOT" --force
elif command -v pipx >/dev/null 2>&1; then
  pipx install --editable "$REPO_ROOT" --force
else
  die "neither uv nor pipx found — install uv (https://docs.astral.sh/uv/) and re-run"
fi
command -v engmem >/dev/null 2>&1 || die "engmem is not on PATH — run \`uv tool update-shell\` and open a new shell"

ENGMEM_PY="$(head -n 1 "$(command -v engmem)" | sed 's/^#!//')"
"$ENGMEM_PY" -c 'import tomllib, engmem' 2>/dev/null \
  || die "cannot find the Python that runs engmem (tried $ENGMEM_PY)"

verify_mcp() {
  local reply
  reply="$(printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"setup","version":"0"}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | "$@" 2>/dev/null)" || return 1
  printf '%s\n' "$reply" | "$ENGMEM_PY" -c '
import json, sys
replies = [json.loads(line) for line in sys.stdin if line.strip()]
tools = [tool["name"] for tool in replies[-1]["result"]["tools"]]
print("    MCP server answers, tools: " + ", ".join(tools))
'
}

add_sandbox_root() {
  "$ENGMEM_PY" - "$CODEX_CONFIG" "$STORE" <<'PY'
import json, sys, tomllib
from pathlib import Path

path, store = Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8") if path.exists() else ""
config = tomllib.loads(text)
section = config.get("sandbox_workspace_write")
if section is not None:
    roots = section.get("writable_roots", [])
    if store in roots:
        print("    store already in writable_roots")
    else:
        print("    [sandbox_workspace_write] already exists — add this by hand:")
        print(f"    writable_roots = {json.dumps([*roots, store])}")
    sys.exit(0)
addition = f"\n[sandbox_workspace_write]\nwritable_roots = [{json.dumps(store, ensure_ascii=False)}]\n"
composed = (text if not text or text.endswith("\n") else text + "\n") + addition
if tomllib.loads(composed)["sandbox_workspace_write"]["writable_roots"] != [store]:
    sys.exit(f"refusing to write {path}: the result does not parse as expected")
backup = path.with_suffix(".toml.bak")
backup.write_text(text, encoding="utf-8")
path.write_text(composed, encoding="utf-8")
print(f"    added the store to writable_roots (backup: {backup})")
PY
}

if [[ $DO_CODEX -eq 1 ]]; then
  step "Codex CLI"
  if command -v codex >/dev/null 2>&1; then
    echo "    found: $(command -v codex)"
  elif [[ $INSTALL_CODEX -eq 0 ]]; then
    echo "    not installed (skipped by --no-codex-install)"
  elif command -v npm >/dev/null 2>&1; then
    npm install -g @openai/codex
  elif command -v brew >/dev/null 2>&1; then
    brew install codex
  else
    die "codex not found and neither npm nor brew is available — install Codex and re-run"
  fi

  step "Wiring engmem into Codex (skills, AGENTS.md rule, MCP server)"
  engmem install --agent codex --store "$STORE"

  if [[ $SANDBOX_ROOT -eq 1 ]]; then
    step "Letting Codex's sandbox write to the store"
    add_sandbox_root
  fi

  step "Checking the MCP server Codex will launch"
  CODEX_MCP_COMMAND=()
  while IFS= read -r -d '' part; do
    CODEX_MCP_COMMAND+=("$part")
  done < <("$ENGMEM_PY" - "$CODEX_CONFIG" <<'PY'
import sys, tomllib
from pathlib import Path
entry = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["mcp_servers"]["engmem"]
sys.stdout.write("".join(part + "\0" for part in [entry["command"], *entry["args"]]))
PY
)
  verify_mcp "${CODEX_MCP_COMMAND[@]}" || die "the engmem MCP server in $CODEX_CONFIG did not answer"
fi

if [[ $DO_CHATGPT -eq 1 ]]; then
  step "Wiring engmem into ChatGPT (Secure MCP Tunnel)"
  CHATGPT_OUT="$(engmem install --agent chatgpt --store "$STORE")"
  printf '%s\n' "$CHATGPT_OUT"
  verify_mcp "$ENGMEM_PY" -m engmem.cli mcp --store "$STORE" \
    || die "\`engmem mcp\` did not answer — the tunnel would have nothing to forward to"

  if command -v tunnel-client >/dev/null 2>&1 && [[ -n "${TUNNEL_ID:-}" && -n "${CONTROL_PLANE_API_KEY:-}" ]]; then
    step "Initialising the tunnel-client profile 'engmem'"
    tunnel-client init --sample sample_mcp_stdio_local --profile engmem \
      --tunnel-id "$TUNNEL_ID" \
      --mcp-command "$("$ENGMEM_PY" -c 'import shlex, sys; print(shlex.join(sys.argv[1:]))' \
        "$ENGMEM_PY" -m engmem.cli mcp --store "$STORE")"
    tunnel-client doctor --profile engmem --explain || true
    echo "    start it with: tunnel-client run --profile engmem"
  else
    echo "    tunnel-client, TUNNEL_ID or CONTROL_PLANE_API_KEY missing — follow the steps above by hand"
  fi
fi

step "Done"
[[ "$STORE" != "$HOME/Developer/engmem" ]] && echo "    add to your shell profile: export ENGMEM_HOME=\"$STORE\""
[[ $DO_CODEX -eq 1 ]] && echo "    Codex: run \`codex\`, check /mcp lists engmem, then try \`\$engmem <task>\`"
[[ $DO_CHATGPT -eq 1 ]] && echo "    ChatGPT: Settings -> Apps & Connectors -> Advanced -> Developer mode, Create app, Connection: Tunnel"
exit 0
