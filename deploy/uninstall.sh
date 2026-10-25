#!/usr/bin/env bash
# Remove heimdall's system install. Needs sudo, which is the point.
#
#   sudo deploy/uninstall.sh            # keeps /etc/heimdall and the decision log
#   sudo deploy/uninstall.sh --purge    # also deletes them and the heimdall user
set -euo pipefail

die() { echo "uninstall.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }
[[ $EUID -eq 0 ]] || die "run with sudo"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

say "Stopping services"
systemctl disable --now heimdall-watchdog.service heimdall.service 2>/dev/null || true
rm -f /etc/systemd/system/heimdall.service /etc/systemd/system/heimdall-watchdog.service
systemctl daemon-reload

say "Removing the Firefox policy entries and XPI"
rm -f /etc/firefox/policies/heimdall.xpi
if [[ -f /etc/firefox/policies/policies.json ]]; then
  python3 - /etc/firefox/policies/policies.json <<'EOF'
import json, os, sys
path = sys.argv[1]
data = json.load(open(path))
pol = data.get("policies", {})
pol.get("ExtensionSettings", {}).pop("heimdall@nocount", None)
if not pol.get("ExtensionSettings"):
    pol.pop("ExtensionSettings", None)
for key in ("DisableSafeMode", "BlockAboutProfiles"):
    pol.pop(key, None)
if pol:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
else:
    os.remove(path)
EOF
fi

say "Removing /opt/heimdall"
rm -rf /opt/heimdall

if [[ $PURGE -eq 1 ]]; then
  say "Purging config, secrets, decision log and the heimdall user"
  rm -rf /etc/heimdall /var/lib/heimdall
  id heimdall >/dev/null 2>&1 && userdel heimdall
else
  echo "    kept /etc/heimdall and /var/lib/heimdall (use --purge to delete)"
fi

echo "Done. Restart Firefox; heimdall is no longer force-installed."
