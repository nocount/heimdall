# heimdall

Watcher app for managing social platform use during focus hours: a Firefox
extension plus a local daemon that judge YouTube videos by their content (a
decision model), not by domain. X is blocked outright during focus hours. A
blocked page can be appealed in one sentence, judged by Claude Haiku 5.5; a
grant opens that one page for 10–40 minutes. See [PLAN.md](PLAN.md) for the design.

**Status:** Phases 0–3 are built: bench, shadow mode, enforcement with
appeals, and lockdown (system services, Firefox policy, root watchdog).

```
daemon/      FastAPI daemon on 127.0.0.1:7878 + `heimdall` CLI (uv project)
extension/   Firefox MV3 extension (heimdall@nocount)
deploy/      signing, install/uninstall scripts, systemd units, Firefox policy
bench/       Phase 0 classifier bake-off scripts; personal data in bench/data/ is gitignored
```

## Secrets

The daemon and bench read these from the environment (in production via
`EnvironmentFile=/etc/heimdall/secrets.env`):

```
OPENROUTER_API_KEY=...    # the classifier (Jev)
ANTHROPIC_API_KEY=...     # the appeal judge (Claude Haiku 5.5)
CF_ACCOUNT_ID=...         # only to bench or fall back to Clef on Workers AI
CF_API_TOKEN=...
```

## Phase 0: classifier bake-off

```bash
uv run --project daemon bench/export_history.py          # YouTube /watch visits, last 60 days
uv run --project daemon bench/fetch_meta.py              # metadata via yt-dlp (resumable)
uv run --project daemon bench/label.py                   # a/b/s per video; likely allows come first
uv run --project daemon bench/run_bench.py --env-file secrets.env --show-errors
```

`run_bench.py` builds requests and combines answers with the daemon's own code.
Accuracy is useless on a history that's mostly blockable, so it reports how many allow labels each threshold keeps at each false-allow count,
and gates on keeping ≥75% of allows with ≤5% false allows, ≥30 labels of each
kind, and p99 latency within the daemon's timeout. Model answers are cached in
`bench/data/results.jsonl`, so re-running after labeling more only calls the
models for new items. Write the chosen thresholds into config.

**Phase 0 outcome (2026-10-23, 23 allow / 173 block labels):** Jev answers
one question per video ("worth watching for someone who wants to learn about
AI, technology, or science?"), allowed at ≥ 0.78, and videos from channels in
`rules.youtube.allow_channels` are allowed without a model call. Together:
20/23 allows kept, 1/173 false allows. No fallback model: if OpenRouter is
down, YouTube fails closed. X isn't classified at all; it's blocked outright.
Re-run once there are ≥30 allow labels (retry failed fetches, or label real
browsing with `heimdall review`).

## Running it

```bash
cp config.example.yaml config.local.yaml   # set db_path somewhere writable
set -a; source secrets.env; set +a
uv run --project daemon heimdall --config config.local.yaml serve
```

In another terminal, run Firefox with the extension in a dev profile:

```bash
cd extension && npm install && npm start
```

With `shadow: false` (the default), a blocked page sends the tab to
heimdall's block page: the reason, and unless it's a feed or Shorts, a box
to appeal in one sentence. Limits are enforced by the daemon, not the model:
≥8 words, 5 appeals a day, a 10-minute cooldown on a page after a denial,
grants of at most 40 minutes. When a grant runs out or focus hours begin,
open tabs are re-checked. Pages sit under a "checking…" curtain (video
paused) until they're allowed. If the daemon is unreachable during focus
hours, pages are blocked (fail closed).

With `shadow: true`, nothing is blocked; the toolbar badge shows the
would-be verdict (✕ = would block, ⏱ = granted, ! = daemon unreachable).

To check how the classifier is doing:

```bash
uv run --project daemon heimdall --config config.local.yaml review blocks   # c/w per decision
uv run --project daemon heimdall --config config.local.yaml review --list -n 50
```

Marking a decision correct or wrong appends it to `bench/data/labels.csv`, so
`run_bench.py` re-tunes thresholds on real browsing.

Set `force_focus: true` in `config.local.yaml` to test outside 9–5.

## Tests

```bash
cd daemon && uv run pytest
cd extension && npx web-ext lint
```

## Lockdown (Phase 3)

Installed, heimdall can't be turned off without `sudo`: the daemon runs as a
system user, Firefox force-installs the extension in every profile and in
private windows, and a root watchdog closes Firefox if the extension goes
quiet (safe mode, a crashed extension) and kills other browsers, during focus
hours only.

1. **Sign the extension.** Get AMO API credentials at
   <https://addons.mozilla.org/developers/addon/api/key/> and put them in
   `amo.env` (gitignored) as `WEB_EXT_API_KEY=...` and `WEB_EXT_API_SECRET=...`.
   Then `deploy/sign.sh`. Unlisted: Mozilla's automated review signs it, and
   it isn't published. Each signing needs a new version in `manifest.json`.
2. **Write a production config**: a copy of `config.local.yaml` with
   `force_focus: null` (install refuses a forced schedule). `db_path` is
   ignored; the service uses /var/lib/heimdall.
3. **Install**, from the repo root, with the dev daemon stopped:

   ```bash
   sudo deploy/install.sh --xpi extension/web-ext-artifacts/heimdall-0.3.0.xpi --config config.prod.yaml
   ```

4. **Restart Firefox**, check `about:policies` and `about:addons`, then work
   through [deploy/BYPASS-TESTS.md](deploy/BYPASS-TESTS.md).

Day to day:

```bash
journalctl -u heimdall -u heimdall-watchdog -f       # decisions, appeals, kills
sudoedit /etc/heimdall/config.yaml && sudo systemctl restart heimdall heimdall-watchdog
sudo -u heimdall env HEIMDALL_CONFIG=/etc/heimdall/config.yaml HEIMDALL_DB=/var/lib/heimdall/heimdall.db \
  /opt/heimdall/venv/bin/heimdall review --list -n 50
sudo deploy/uninstall.sh                             # the way back out (--purge to delete everything)
```

Run `review` as the `heimdall` user, not plain `sudo`: root opening the
database while the daemon is stopped can leave root-owned SQLite files the
daemon can't write. To label as you review, pass `--labels
/var/lib/heimdall/labels.csv`, then copy that file into `bench/data/`.

Developing after lockdown: the installed daemon owns port 7878, so stop it
(`sudo systemctl stop heimdall-watchdog heimdall`) before running a dev
daemon, and the force-installed extension is also present in `web-ext`
profiles. To ship an extension change, bump the version, re-sign, and re-run
install.sh.
