#!/usr/bin/env bash
# Install heimdall system-wide (PLAN.md Phase 3). Re-run it to upgrade.
#
#   sudo deploy/install.sh --xpi extension/web-ext-artifacts/heimdall-0.2.0.xpi \
#                          [--config config.yaml] [--secrets secrets.env] [--uv /path/to/uv]
#
# What it does:
#   - creates the `heimdall` system user
#   - copies the daemon to /opt/heimdall (root-owned) with its own venv,
#     dependencies pinned from daemon/uv.lock
#   - /etc/heimdall/config.yaml (root:heimdall 0640) and secrets.env (root 0600);
#     an existing config is kept unless --config is given
#   - systemd units heimdall.service (user heimdall) and
#     heimdall-watchdog.service (root), enabled and started
#   - the signed XPI and policies.json in /etc/firefox/policies, merged into
#     any existing policies.json
#
# Undo with deploy/uninstall.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OPT=/opt/heimdall
ETC=/etc/heimdall
FFPOL=/etc/firefox/policies
EXT_ID="heimdall@nocount"

die() { echo "install.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }

XPI="" CONFIG="" SECRETS="$REPO/secrets.env" UV="" ALLOW_FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --xpi) XPI="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --secrets) SECRETS="$2"; shift 2 ;;
    --uv) UV="$2"; shift 2 ;;
    --allow-force-focus) ALLOW_FORCE=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$XPI" ]] || die "--xpi is required (sign it first: deploy/sign.sh)"
[[ -f "$XPI" ]] || die "no such XPI: $XPI"
[[ -f "$SECRETS" ]] || die "no secrets file at $SECRETS (see README: Secrets)"
if [[ -z "$UV" ]]; then
  UV="$(command -v uv || true)"
  [[ -n "$UV" ]] || UV="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/.local/bin/uv"
fi
[[ -x "$UV" ]] || die "uv not found; pass --uv /path/to/uv"

# --- preflight -----------------------------------------------------------------

say "Checking the XPI"
python3 - "$XPI" "$EXT_ID" <<'EOF' || die "XPI check failed"
import json, sys, zipfile
xpi, ext_id = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(xpi) as z:
    names = set(z.namelist())
    manifest = json.loads(z.read("manifest.json"))
got = manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id")
if got != ext_id:
    sys.exit(f"extension id is {got!r}, expected {ext_id!r}")
if not any(n.startswith("META-INF/") and n.endswith((".rsa", ".sig", "cose.sig")) for n in names):
    sys.exit("XPI isn't signed (no META-INF signature); release Firefox won't load it. Run deploy/sign.sh")
print(f"    {ext_id} {manifest['version']}, signed")
EOF

if ss -ltnpH 'sport = :7878' 2>/dev/null | grep -q . && ! systemctl -q is-active heimdall.service; then
  die "something else is listening on 127.0.0.1:7878 (a dev daemon?). Stop it first."
fi

grep -q '^OPENROUTER_API_KEY=.' "$SECRETS" || die "$SECRETS has no OPENROUTER_API_KEY"
grep -q '^ANTHROPIC_API_KEY=.' "$SECRETS" || echo "    warning: no ANTHROPIC_API_KEY in $SECRETS; appeals will be disabled"

# --- user ------------------------------------------------------------------------

if ! id heimdall >/dev/null 2>&1; then
  say "Creating system user heimdall"
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin heimdall
fi

# --- code and venv -----------------------------------------------------------------

say "Installing the daemon to $OPT"
install -d -o root -g root -m 0755 "$OPT"
rm -rf "$OPT/src"
install -d -o root -g root -m 0755 "$OPT/src"
cp -r "$REPO/daemon/heimdall" "$REPO/daemon/pyproject.toml" "$REPO/daemon/uv.lock" "$OPT/src/"
cp "$REPO/README.md" "$OPT/"
find "$OPT/src" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$OPT"

rm -rf "$OPT/venv"
"$UV" venv --quiet --python /usr/bin/python3 "$OPT/venv"
(cd "$OPT/src" && "$UV" export --quiet --frozen --no-dev --no-emit-project -o "$OPT/requirements.txt")
"$UV" pip install --quiet --python "$OPT/venv/bin/python" -r "$OPT/requirements.txt"
"$UV" pip install --quiet --python "$OPT/venv/bin/python" --no-deps "$OPT/src"
chmod -R go-w "$OPT"

# --- config and secrets --------------------------------------------------------------

say "Writing $ETC"
install -d -o root -g heimdall -m 0750 "$ETC"
if [[ -n "$CONFIG" ]]; then
  install -o root -g heimdall -m 0640 "$CONFIG" "$ETC/config.yaml"
elif [[ ! -f "$ETC/config.yaml" ]]; then
  install -o root -g heimdall -m 0640 "$REPO/config.example.yaml" "$ETC/config.yaml"
  echo "    no --config given: installed config.example.yaml (no channel allowlist)"
else
  echo "    keeping the existing $ETC/config.yaml"
fi
"$OPT/venv/bin/python" - "$ETC/config.yaml" "$ALLOW_FORCE" <<'EOF' || die "config check failed"
import sys
from heimdall.config import load_config
c = load_config(sys.argv[1])
if c.force_focus is not None and sys.argv[2] != "1":
    sys.exit(f"config sets force_focus: {c.force_focus} (a dev setting). Set it to null, or pass --allow-force-focus")
if c.shadow:
    print("    note: shadow: true; nothing will be blocked")
print(f"    classifier {c.classifier.provider}:{c.classifier.model}, "
      f"{len(c.rules.youtube.allow_channels)} allowlisted channels")
EOF
install -o root -g root -m 0600 "$SECRETS" "$ETC/secrets.env"

# --- services ------------------------------------------------------------------------

say "Starting heimdall.service"
install -m 0644 "$REPO/deploy/heimdall.service" "$REPO/deploy/heimdall-watchdog.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet heimdall.service heimdall-watchdog.service
systemctl restart heimdall.service
for _ in $(seq 30); do
  curl -sf http://127.0.0.1:7878/v1/status >/dev/null && break
  sleep 0.5
done
curl -sf http://127.0.0.1:7878/v1/status >/dev/null \
  || die "the daemon didn't come up; see: journalctl -u heimdall -n 50"

# --- Firefox policy ----------------------------------------------------------------------

say "Installing the Firefox policy and XPI to $FFPOL"
install -d -m 0755 "$FFPOL"
install -m 0644 "$XPI" "$FFPOL/heimdall.xpi"
python3 - "$FFPOL/policies.json" "$REPO/deploy/policies.json" <<'EOF'
import json, os, shutil, sys, time
path, ours_path = sys.argv[1], sys.argv[2]
ours = json.load(open(ours_path))["policies"]
current = json.load(open(path)) if os.path.exists(path) else {"policies": {}}
merged = json.loads(json.dumps(current))
pol = merged.setdefault("policies", {})
pol.setdefault("ExtensionSettings", {}).update(ours["ExtensionSettings"])
for k, v in ours.items():
    if k != "ExtensionSettings":
        pol[k] = v
if merged != current:
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    os.chmod(path, 0o644)
EOF

# --- watchdog last -------------------------------------------------------------------------

say "Starting heimdall-watchdog.service"
systemctl restart heimdall-watchdog.service

cat <<EOF

heimdall is installed.

  1. Restart Firefox now. The policy (and the extension) load at startup, and
     during focus hours the watchdog closes a Firefox that isn't running
     heimdall within a couple of minutes. Session restore brings tabs back.
  2. Check about:policies (Active) and about:addons (heimdall, no Disable or
     Remove buttons).
  3. Work through the bypass checklist: deploy/BYPASS-TESTS.md

Logs:    journalctl -u heimdall -u heimdall-watchdog -f
Config:  sudoedit $ETC/config.yaml && sudo systemctl restart heimdall heimdall-watchdog
Upgrade: re-sign with a new version, then re-run this script.
EOF
