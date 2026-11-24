#!/usr/bin/env bash
# Install tex-annotator for the current user (macOS / Linux). No admin rights needed.
#
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove it again
#
# It copies the app to ~/.local/share/tex-annotator and puts a `tex-annotator`
# command in ~/.local/bin. Nothing else on your system is changed.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${TEX_ANNOTATOR_HOME:-$HOME/.local/share/tex-annotator}"
BIN_DIR="${TEX_ANNOTATOR_BIN:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/tex-annotator"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$PREFIX"
  rm -f "$LAUNCHER"
  echo "tex-annotator removed. (Logs and deleted-file backups are in ~/.cache/tex-annotator if you want to remove those too.)"
  exit 0
fi

[ -f "$SRC/tex_annotator/app.py" ] || fail "Run this script from the unzipped tex-annotator folder."

# Python 3.10+
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer is needed. macOS: 'brew install python' · Ubuntu/Debian: 'sudo apt install python3'."

bold "Installing tex-annotator…"
rm -rf "$PREFIX"
mkdir -p "$PREFIX" "$BIN_DIR"
cp -R "$SRC/tex_annotator" "$PREFIX/"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$LAUNCHER" <<EOF
#!/bin/sh
# tex-annotator launcher (created by install.sh)
PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PY" -m tex_annotator "\$@"
EOF
chmod +x "$LAUNCHER"
echo "  app:     $PREFIX"
echo "  command: $LAUNCHER"
echo

bold "Checking requirements…"
"$LAUNCHER" --check || true
echo

# Make sure ~/.local/bin is on PATH.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    case "${SHELL:-}" in
      */zsh)  RC="$HOME/.zshrc" ;;
      */bash) if [ "$(uname)" = "Darwin" ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
      *)      RC="$HOME/.profile" ;;
    esac
    LINE="export PATH=\"$BIN_DIR:\$PATH\""
    bold "One more step: $BIN_DIR isn't on your PATH yet."
    if [ -t 0 ]; then
      printf 'Add it to %s now? [Y/n] ' "$RC"
      read -r answer || answer=""
      case "$answer" in
        n|N|no|No) echo "OK. Add this line to $RC yourself:"; echo "  $LINE" ;;
        *) printf '\n# Added by tex-annotator install.sh\n%s\n' "$LINE" >> "$RC"
           echo "Added. Open a new terminal (or run: source $RC) before using tex-annotator." ;;
      esac
    else
      echo "Add this line to $RC, then open a new terminal:"
      echo "  $LINE"
    fi
    echo
    ;;
esac

bold "Done. To start:"
echo "  cd path/to/your/latex/project"
echo "  tex-annotator"
