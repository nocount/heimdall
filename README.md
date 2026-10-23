# heimdall

Watcher app for managing social platform use during focus hours: a Firefox
extension plus a local daemon that judge YouTube videos by their content (a
decision model), not by domain. X is blocked outright during focus hours; an
appeal (Phase 2) opens a single post or profile. See [PLAN.md](PLAN.md) for the design.

**Status:** Phase 0 (bench) and Phase 1 (shadow mode) are built. Nothing is
blocked yet; the daemon logs what it *would* have done.

```
daemon/      FastAPI daemon on 127.0.0.1:7878 + `heimdall` CLI (uv project)
extension/   Firefox MV3 extension (heimdall@nocount)
bench/       Phase 0 classifier bake-off scripts; personal data in bench/data/ is gitignored
```

## Secrets

The daemon and bench read these from the environment (in production via
`EnvironmentFile=/etc/heimdall/secrets.env`):

```
CF_ACCOUNT_ID=...
CF_API_TOKEN=...          # Workers AI scope
OPENROUTER_API_KEY=...    # primary classifier (Jev); Workers AI Clef is the fallback
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

**Phase 0 outcome (2026-10-23, 10 allow / 143 block labels):** YouTube is
judged on `educational` alone; gating on content type blocked the AI
interviews and podcasts you allow. Jev is primary at `educational ≥ 0.81`
(kept 7/10 allows, 0 false allows, p99 976ms), with Clef on Workers AI as
the fallback at 0.76. Re-run once there are ≥30 allow labels.

## Phase 1: shadow mode

```bash
cp config.example.yaml config.local.yaml   # set db_path somewhere writable
set -a; source secrets.env; set +a
uv run --project daemon heimdall --config config.local.yaml serve
```

In another terminal, run Firefox with the extension in a dev profile:

```bash
cd extension && npm install && npm start
```

The toolbar badge shows the would-be verdict (✕ = would block, ! = daemon
unreachable); hover it for the reason. After a few days of normal use:

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
