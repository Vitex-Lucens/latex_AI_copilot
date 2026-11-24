#!/usr/bin/env bash
# tex-annotator installer for macOS and Linux (Ubuntu/Debian, other distributions, and WSL on Windows).
#
#   curl -fsSL https://raw.githubusercontent.com/Vitex-Lucens/latex_AI_copilot/main/install.sh | bash
#   ./install.sh               from a copy of the repository (git clone or zip)
#   ./install.sh --uninstall   remove the app (TeX, Ghostscript and Claude Code stay installed)
#
# It installs whatever is missing (Python 3.10+, TeX with latexmk, Ghostscript, Claude Code), asking
# before each step, then the app itself: into ~/.local/share/tex-annotator, with a `tex-annotator`
# command in ~/.local/bin. Run it again at any time to update. The app needs no admin rights; installing
# TeX and the other tools asks for your password.
#
# Settings (environment variables):
#   TEX_ANNOTATOR_YES=1            answer yes to every question
#   TEX_ANNOTATOR_SKIP_PREREQS=1   install only the app
#   TEX_ANNOTATOR_NO_CLAUDE=1      don't offer to install Claude Code
#   TEX_ANNOTATOR_REF=<branch|tag> the version to download (default: main)
#   TEX_ANNOTATOR_SOURCE=<folder or .tar.gz URL>   install from there instead of GitHub
set -euo pipefail

REPO="Vitex-Lucens/latex_AI_copilot"
REF="${TEX_ANNOTATOR_REF:-main}"
PREFIX="${TEX_ANNOTATOR_HOME:-$HOME/.local/share/tex-annotator}"
BIN_DIR="${TEX_ANNOTATOR_BIN:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/tex-annotator"
ORIG_PATH="$PATH"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Questions go to the terminal, so they also work when this script is piped into bash.
can_ask() { (true </dev/tty) 2>/dev/null; }
ask() {
  [ "${TEX_ANNOTATOR_YES:-}" = 1 ] && return 0
  if ! can_ask; then
    note "(No terminal to ask in, so skipping this. Set TEX_ANNOTATOR_YES=1 to install without asking.)"
    return 1
  fi
  local answer=""
  printf '%s [Y/n] ' "$1" >/dev/tty
  read -r answer </dev/tty || answer=n
  case "$answer" in [nN]*) return 1 ;; *) return 0 ;; esac
}

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$PREFIX"
  rm -f "$LAUNCHER"
  echo "tex-annotator removed. (Its logs and deleted-file backups are in ~/.cache/tex-annotator if you want those gone too.)"
  exit 0
fi

case "$(uname -s)" in
  Darwin) PLATFORM=mac ;;
  Linux) PLATFORM=linux ;;
  *) fail "This installer is for macOS and Linux. On Windows, use install.ps1 (see the README)." ;;
esac
WSL=""
if [ "$PLATFORM" = linux ] && grep -qi microsoft /proc/version 2>/dev/null; then WSL=1; fi
if [ "$PLATFORM" = mac ] && [ -d /Library/TeX/texbin ]; then export PATH="/Library/TeX/texbin:$PATH"; fi
export PATH="$BIN_DIR:$HOME/.local/bin:$PATH"

if [ "$PLATFORM" = mac ]; then bold "tex-annotator installer (macOS)"; else bold "tex-annotator installer (Linux${WSL:+ on WSL})"; fi

# ------------------------------------------------------------------------- the app's files
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
if [ -n "${TEX_ANNOTATOR_SOURCE:-}" ] && [ -d "$TEX_ANNOTATOR_SOURCE" ]; then
  SRC="$(cd "$TEX_ANNOTATOR_SOURCE" && pwd)"
elif [ -n "$HERE" ] && [ -f "$HERE/tex_annotator/app.py" ]; then
  SRC="$HERE"
else
  URL="${TEX_ANNOTATOR_SOURCE:-https://github.com/$REPO/archive/$REF.tar.gz}"
  bold "Downloading tex-annotator…"
  if ! curl -fsSL "$URL" | tar -xz -C "$TMP"; then
    fail "Couldn't download $URL. Check your internet connection. (If the repository is private: git clone git@github.com:$REPO.git, then run ./install.sh in that folder.)"
  fi
  APP="$(find "$TMP" -path '*/tex_annotator/app.py' | head -n 1)"
  [ -n "$APP" ] || fail "The download doesn't contain the app."
  SRC="$(dirname "$(dirname "$APP")")"
fi

# ------------------------------------------------------------------------- what it needs
PY=""
find_python() {
  PY=""
  local cand
  for cand in python3 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if have "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"
      return 0
    fi
  done
  return 1
}

tex_ok() { have latexmk && have synctex && have kpsewhich && kpsewhich tikz.sty >/dev/null 2>&1; }

ensure_brew() {
  have brew && return 0
  local b
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$b" ]; then eval "$("$b" shellenv)"; return 0; fi
  done
  ask "Homebrew (the macOS package manager) is needed to install the rest. Install it now? It asks for your password." || return 1
  NONINTERACTIVE="${TEX_ANNOTATOR_YES:+1}" /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$b" ]; then eval "$("$b" shellenv)"; return 0; fi
  done
  return 1
}

prereqs_mac() {
  find_python || true
  if [ -n "$PY" ] && tex_ok && have gs; then return 0; fi
  if ! ensure_brew; then
    warn "Without Homebrew, install these yourself: Python 3.10+ (python.org), MacTeX (tug.org/mactex), Ghostscript."
    return 0
  fi
  if [ -z "$PY" ] && ask "Install Python 3 with Homebrew?"; then
    brew install python
    find_python || true
  fi
  if ! tex_ok; then
    note "TeX isn't installed (or is missing parts). MacTeX is the standard TeX for macOS: a big download, about 5 GB."
    if ask "Install MacTeX with Homebrew? It asks for your password."; then
      brew install --cask mactex-no-gui
      export PATH="/Library/TeX/texbin:$PATH"
    fi
  fi
  if ! have gs && ask "Install Ghostscript (needed to import PDFs) with Homebrew?"; then
    brew install ghostscript
  fi
}

prereqs_linux() {
  find_python || true
  local pkgs=()
  [ -n "$PY" ] || pkgs+=(python3)
  { tex_ok && kpsewhich tcolorbox.sty >/dev/null 2>&1; } || pkgs+=(texlive-latex-extra texlive-fonts-recommended tex-gyre lmodern latexmk)
  have gs || pkgs+=(ghostscript)
  have curl || pkgs+=(curl)
  [ ${#pkgs[@]} -eq 0 ] && return 0
  if have apt-get; then
    bold "Missing: ${pkgs[*]}"
    note "They install with: sudo apt-get install ${pkgs[*]}"
    case " ${pkgs[*]} " in *" texlive-latex-extra "*) note "(TeX is a large download, about 1 GB.)" ;; esac
    if ask "Install them now? It asks for your password."; then
      sudo apt-get update
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
    fi
  else
    warn "Install these with your package manager: Python 3.10+, TeX with latexmk, Ghostscript."
    note "Fedora: sudo dnf install python3 latexmk texlive-scheme-medium ghostscript"
    note "Arch:   sudo pacman -S python texlive-latexextra texlive-fontsrecommended texlive-binextra ghostscript"
  fi
  find_python || true
}

prereq_claude() {
  if have claude || [ -x "$HOME/.local/bin/claude" ] || [ "${TEX_ANNOTATOR_NO_CLAUDE:-}" = 1 ]; then return 0; fi
  note "Claude Code is what does the work in tex-annotator; it uses your own Claude account."
  if ask "Install Claude Code with Anthropic's official installer?"; then
    curl -fsSL https://claude.ai/install.sh | bash
  fi
}

if [ "${TEX_ANNOTATOR_SKIP_PREREQS:-}" != 1 ]; then
  bold "Checking what tex-annotator needs…"
  if [ "$PLATFORM" = mac ]; then prereqs_mac; else prereqs_linux; fi
  prereq_claude
fi
find_python || true
[ -n "$PY" ] || fail "Python 3.10 or newer is needed. macOS: brew install python · Ubuntu/Debian: sudo apt install python3 (on Ubuntu 20.04, python3.10 from the deadsnakes PPA)."

# ------------------------------------------------------------------------- the app
bold "Installing tex-annotator…"
mkdir -p "$BIN_DIR"
rm -rf "$PREFIX.new"
mkdir -p "$PREFIX.new"
cp -R "$SRC/tex_annotator" "$PREFIX.new/"
for f in README.md LICENSE THIRD_PARTY_NOTICES.md install.sh; do
  if [ -f "$SRC/$f" ]; then cp "$SRC/$f" "$PREFIX.new/"; fi
done
find "$PREFIX.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$PREFIX"
mv "$PREFIX.new" "$PREFIX"

cat > "$LAUNCHER" <<EOF
#!/bin/sh
# tex-annotator launcher (created by install.sh)
PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PY" -X utf8 -m tex_annotator "\$@"
EOF
chmod +x "$LAUNCHER"
note "app:     $PREFIX"
note "command: $LAUNCHER  (using $PY)"
echo

# ~/.local/bin on PATH for new terminals (Claude Code's installer puts `claude` there too)
case ":$ORIG_PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    if ! grep -qs "$BIN_DIR" "$HOME/.profile" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc"; then
      case "${SHELL:-}" in
        */zsh) RC="$HOME/.zshrc" ;;
        */bash) if [ "$PLATFORM" = mac ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
        *) RC="$HOME/.profile" ;;
      esac
      LINE="export PATH=\"$BIN_DIR:\$PATH\""
      if ask "Add $BIN_DIR to your PATH in $RC, so the tex-annotator command works in new terminals?"; then
        printf '\n# Added by the tex-annotator installer\n%s\n' "$LINE" >> "$RC"
        note "Added. Open a new terminal before using tex-annotator."
      else
        note "Add this line to $RC yourself: $LINE"
      fi
      echo
    fi
    ;;
esac

bold "Checking the setup…"
CHECK=0
"$LAUNCHER" --check || CHECK=$?
echo
bold "Done. To start, open a new terminal and run:"
note "cd path/to/your/latex/project && tex-annotator"
note "or, to rebuild a PDF as LaTeX:   tex-annotator path/to/file.pdf"
if [ "$CHECK" != 0 ]; then
  note "Something above is still missing. Run this installer again, or install it yourself (see the README)."
  note "If Claude Code is new here, run \`claude\` once and sign in."
fi
