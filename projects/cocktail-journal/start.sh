#!/usr/bin/env sh
# Open the journal in your default browser.
# No build step, no dependencies.

DIR=$(cd "$(dirname "$0")" && pwd)

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$DIR/index.html"
elif command -v open >/dev/null 2>&1; then
  open "$DIR/index.html"
else
  echo "Please open $DIR/index.html manually."
fi
