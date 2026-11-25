#!/usr/bin/env bash
# Build dist/latex-copilot-<version>.zip: the folder to send to people.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
VERSION="$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+?)\"", open("latex_copilot/__init__.py").read()).group(1))')"
NAME="latex-copilot-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME" dist
cp -R latex_copilot docs install.sh install.ps1 install.cmd README.md LICENSE SECURITY.md THIRD_PARTY_NOTICES.md pyproject.toml "$STAGE/$NAME/"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "$STAGE/$NAME/install.sh"
rm -f "dist/$NAME.zip"
(cd "$STAGE" && zip -qr "$OLDPWD/dist/$NAME.zip" "$NAME")
echo "Built dist/$NAME.zip ($(du -h "dist/$NAME.zip" | cut -f1))"
