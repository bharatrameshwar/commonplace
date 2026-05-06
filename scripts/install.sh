#!/usr/bin/env bash
# Install Commonplace launchd agents.
#
# Usage:
#   ./scripts/install.sh           # install/update all agents
#   ./scripts/install.sh --uninstall  # remove all agents
#
# Substitutes __INSTALL_DIR__, __DATA_DIR__, __VENV_PYTHON__ in the
# .plist.template files and writes them into ~/Library/LaunchAgents.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${HOME}/.local/share/commonplace"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
TEMPLATE_DIR="${REPO_DIR}/scripts/launchd"

AGENTS=(
  "com.commonplace.tracker"
  "com.commonplace.dashboard"
  "com.commonplace.classifier"
  "com.commonplace.cleanup"
)

uninstall() {
  for agent in "${AGENTS[@]}"; do
    plist="${LAUNCH_AGENTS_DIR}/${agent}.plist"
    if [[ -f "$plist" ]]; then
      echo "Unloading $agent"
      launchctl bootout "gui/$(id -u)/${agent}" 2>/dev/null || true
      rm -f "$plist"
    fi
  done
  echo "Uninstalled."
}

install_agents() {
  if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
    echo "ERROR: ${REPO_DIR}/.venv/bin/python not found." >&2
    echo "Create a venv first:" >&2
    echo "  cd ${REPO_DIR} && python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
  fi

  mkdir -p "$DATA_DIR" "$LAUNCH_AGENTS_DIR"

  local venv_python="${REPO_DIR}/.venv/bin/python"

  for agent in "${AGENTS[@]}"; do
    template="${TEMPLATE_DIR}/${agent}.plist.template"
    target="${LAUNCH_AGENTS_DIR}/${agent}.plist"
    if [[ ! -f "$template" ]]; then
      echo "WARN: template missing: $template" >&2
      continue
    fi

    # Already running? Bootout first so we can reload.
    launchctl bootout "gui/$(id -u)/${agent}" 2>/dev/null || true

    sed \
      -e "s|__INSTALL_DIR__|${REPO_DIR}|g" \
      -e "s|__DATA_DIR__|${DATA_DIR}|g" \
      -e "s|__VENV_PYTHON__|${venv_python}|g" \
      "$template" > "$target"

    launchctl bootstrap "gui/$(id -u)" "$target"
    echo "Installed $agent"
  done

  echo
  echo "Done. Logs in: $DATA_DIR"
  echo "Dashboard:    http://127.0.0.1:8420"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall
else
  install_agents
fi
