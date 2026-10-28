#!/bin/sh
# Prints an instance folder as one base64 line, for the INSTANCE_TAR secret of the GitHub Actions workflow.
# Packs config.toml, .env, prompts/ and custom.py; never cache/, logs/ or docs/. GitHub secrets hold up to 48 KB.
# Usage: scripts/instance_secret.sh instances/mine | pbcopy
set -e
cd "${1:?usage: scripts/instance_secret.sh <instance folder>}"
FILES=""
for f in config.toml .env prompts custom.py; do [ -e "$f" ] && FILES="$FILES $f"; done
OUT="$(tar czf - $FILES | base64 | tr -d '\n')"
SIZE=$(printf '%s' "$OUT" | wc -c)
[ "$SIZE" -le 49152 ] || { echo "Too big for a GitHub secret: $SIZE bytes (max 49152)" >&2; exit 1; }
printf '%s\n' "$OUT"
