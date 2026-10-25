#!/usr/bin/env bash
# Sign the extension for self-distribution (unlisted on addons.mozilla.org).
# Release Firefox only installs signed extensions, even via enterprise policy.
#
#   deploy/sign.sh            # run as yourself, not root
#
# Needs AMO API credentials (https://addons.mozilla.org/developers/addon/api/key/)
# in the environment or in amo.env at the repo root (gitignored):
#   WEB_EXT_API_KEY=user:12345:67
#   WEB_EXT_API_SECRET=...
#
# Unlisted means signed but not published: Mozilla's automated review checks
# it, and nobody can find it on AMO. Every submission needs a new version in
# extension/manifest.json. The signed XPI lands in extension/web-ext-artifacts/.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
die() { echo "sign.sh: $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "run as yourself, not root"
if [[ -f "$REPO/amo.env" ]]; then
  # shellcheck disable=SC1091
  set -a; . "$REPO/amo.env"; set +a
fi
[[ -n "${WEB_EXT_API_KEY:-}" && -n "${WEB_EXT_API_SECRET:-}" ]] \
  || die "set WEB_EXT_API_KEY and WEB_EXT_API_SECRET (or put them in $REPO/amo.env)"

# shellcheck disable=SC1091
[[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]] && . "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null
command -v npx >/dev/null || die "npx not found (node via nvm?)"

cd "$REPO/extension"
version="$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')"
echo "Signing heimdall $version (unlisted)..."
npx web-ext lint
npx web-ext sign --channel unlisted

xpi="$(ls -t web-ext-artifacts/*.xpi 2>/dev/null | head -1)"
[[ -n "$xpi" ]] || die "web-ext didn't produce an XPI"
echo
echo "Signed: $REPO/extension/$xpi"
echo "Install it with: sudo deploy/install.sh --xpi extension/$xpi"
