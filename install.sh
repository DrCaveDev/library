#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — Download and install the library skill
#
# Fetches the latest `main` branch of github.com/DrCaveDev/library and
# copies it into one or more install locations. Supports pi
# (~/.pi/agent/skills/library), Claude Code (~/.claude), and custom paths.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_URL="https://github.com/DrCaveDev/library.git"
REF="main"
TEMP_DIR=$(mktemp -d /tmp/library-install-XXXXXX)
trap 'rm -rf "$TEMP_DIR"' EXIT

# --- Color helpers (only if terminal supports it) ---
if [[ -t 1 ]]; then
  BOLD="\033[1m"
  GREEN="\033[0;32m"
  YELLOW="\033[0;33m"
  CYAN="\033[0;36m"
  NC="\033[0m" # No Color
else
  BOLD=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
fi

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
header(){ echo -e "\n${BOLD}${CYAN}── $* ──${NC}\n"; }

# Read from /dev/tty so prompts work even when piped via curl | bash
tty_read() {
  if [[ -r /dev/tty ]]; then
    read "$@" </dev/tty
  else
    read "$@"
  fi
}

# ---------------------------------------------------------------------------
# 1. Fetch the repo
# ---------------------------------------------------------------------------
header "Downloading library skill from GitHub"
git clone --depth 1 --branch "$REF" "$REPO_URL" "$TEMP_DIR" --quiet
rm -rf "$TEMP_DIR/.git"
info "Fetched $REPO_URL @ $REF"

# ---------------------------------------------------------------------------
# 2. Choose install destinations (multi-select)
# ---------------------------------------------------------------------------
header "Choose install destination(s)"

DEST_DIRS=()

# Pre-defined options
OPT_PI="~/.pi/agent/skills/library"
OPT_CLAUDE="~/.claude/library"

echo "Enter the numbers of the locations to install to (space-separated),"
echo "or enter a custom path."
echo ""
echo "  1) $OPT_PI      (pi agent skill)"
echo "  2) $OPT_CLAUDE   (Claude Code skill)"
echo "  3) Custom path"
echo ""

tty_read -rp "Choice(s): " -a CHOICES

for choice in "${CHOICES[@]}"; do
  case "$choice" in
    1) DEST_DIRS+=("$OPT_PI") ;;
    2) DEST_DIRS+=("$OPT_CLAUDE") ;;
    3)
      tty_read -rp "  Enter custom install path: " custom_path
      if [[ -n "$custom_path" ]]; then
        # Expand ~ if present
        DEST_DIRS+=("${custom_path/#\~/$HOME}")
      else
        warn "No custom path entered — skipping."
      fi
      ;;
    *)
      # Could be a path typed directly
      DEST_DIRS+=("${choice/#\~/$HOME}")
      ;;
  esac
done

if [[ ${#DEST_DIRS[@]} -eq 0 ]]; then
  echo "No destinations selected. Nothing to do."
  exit 0
fi

# Deduplicate
mapfile -t DEST_DIRS < <(printf "%s\n" "${DEST_DIRS[@]}" | sort -u)

# ---------------------------------------------------------------------------
# 3. Install to each destination
# ---------------------------------------------------------------------------
for dest in "${DEST_DIRS[@]}"; do
  # Expand ~ if not already expanded
  dest="${dest/#\~/$HOME}"
  header "Installing to $dest"

  if [[ -d "$dest" ]]; then
    echo "  Destination exists: $dest"
    tty_read -rp "  [o]verwrite / [s]kip: " overwrite_choice
    case "$overwrite_choice" in
      o|O)
        rm -rf "$dest"
        mkdir -p "$(dirname "$dest")"
        cp -R "$TEMP_DIR"/. "$dest"
        info "Installed to $dest"
        ;;
      *)
        warn "Skipped $dest"
        ;;
    esac
  else
    mkdir -p "$(dirname "$dest")"
    cp -R "$TEMP_DIR"/. "$dest"
    info "Installed to $dest"
  fi
done

# ---------------------------------------------------------------------------
# 4. Summary
# ---------------------------------------------------------------------------
header "Done"
echo "Installed to:"
for dest in "${DEST_DIRS[@]}"; do
  echo "  • $dest"
done

echo ""
echo "Quick start:"
echo "  python3 \"$dest/scripts/library.py\" validate"
echo "  python3 \"$dest/scripts/library.py\" install"
echo ""
