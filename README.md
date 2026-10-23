# heimdall

Watcher app for managing social platform use during focus hours: a Firefox
extension plus a local daemon that judge YouTube and X pages by their content
(decision model on Workers AI), not by domain. See [PLAN.md](PLAN.md) for the design.

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
OPENROUTER_API_KEY=...    # bench only (Jev, Microsoft-Decision-1)
```

## Phase 0: classifier bake-off

```bash
uv run --project daemon bench/export_history.py          # YouTube /watch visits, last 60 days
uv run --project daemon bench/fetch_meta.py --limit 150  # metadata via yt-dlp (resumable)
uv run --project daemon bench/label.py                   # a/b/s per video; aim for ~80, mixed
uv run --project daemon bench/label.py --x-csv bench/data/x_samples.csv   # format: x_samples.example.csv
uv run --project daemon bench/run_bench.py --env-file secrets.env --show-errors
```

`run_bench.py` builds requests and combines answers with the daemon's own code,
reports accuracy / false-allow / false-block per site, sweeps thresholds, and
checks the gate (≥90% accuracy, ≤5% false-allow, p95 < 300ms). Model answers
are cached in `bench/data/results.jsonl`, so re-running after labeling more
only calls the models for new items. Write the winning thresholds into config.

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
