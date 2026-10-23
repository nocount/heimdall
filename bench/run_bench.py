"""Run every labeled item through each decision model and report against your labels.

    uv run --project daemon bench/run_bench.py --env-file /etc/heimdall/secrets.env
    uv run --project daemon bench/run_bench.py --models workers_ai:@cf/cloudflare/clef-flash \\
        local_llamacpp:clef-flash@http://127.0.0.1:8081

Requests are built and verdicts combined by the daemon's own code
(questions.build_request, policy.apply_rules), so the numbers predict the
daemon. Answers are cached in bench/data/results.jsonl; rerunning only calls
the models for new labels. --fresh ignores the cache.

Labels are lopsided (most of what you watch should be blocked), so accuracy
is meaningless: blocking everything scores >90%. Each site is instead judged
by how many allow labels it keeps at a false-allow budget. False allows leak
(a leak defeats the tool); false blocks are friction, but appealable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

import httpx

from heimdall import labels, pages
from heimdall.config import Config, ProviderConfig, Thresholds, load_config
from heimdall.models.systemone import SystemOneError, make_provider
from heimdall.policy import apply_rules
from heimdall.questions import build_request

DATA = Path(__file__).parent / "data"
DEFAULT_MODELS = [
    "workers_ai:@cf/cloudflare/clef-flash",
    "openrouter:typesafe/jev-1.13",
    "openrouter:microsoft/microsoft-decision-1",
]
REPO = Path(__file__).resolve().parents[1]
GATE = {
    "false_allow": 0.05,  # share of block labels let through
    "kept_allow": 0.75,   # share of allow labels let through, within the false-allow budget
    "min_labels": 30,     # per class and site; below this, rates move in big jumps
}
# The one score each site's rule thresholds on (policy.apply_rules).
SCORE_KEY = {"youtube": "educational"}


@dataclass
class Item:
    id: str
    site: str
    label: str
    state: object
    questions: dict
    key: str  # hash of the exact request, so changed questions invalidate the cache


def parse_model(spec: str) -> ProviderConfig:
    """provider:model[@url]"""
    provider, _, rest = spec.partition(":")
    model, _, url = rest.partition("@") if provider == "local_llamacpp" else (rest, "", "")
    return ProviderConfig(provider=provider, model=model, url=url or None, timeout_ms=10_000)


def load_env_file(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_items(rows: dict) -> tuple[list[Item], list[str]]:
    items, skipped = [], []
    for row in rows.values():
        page = pages.parse(row["url"])
        try:
            state, questions = build_request(page, row["metadata"])
        except ValueError as e:
            skipped.append(f"{row['id']}: {e}")
            continue
        key = hashlib.sha256(json.dumps([state, questions], sort_keys=True).encode()).hexdigest()[:16]
        items.append(Item(row["id"], row["site"], row["label"], state, questions, key))
    return items, skipped


def load_results(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            out[(r["model"], r["key"])] = r
    return out


async def run_model(cfg: ProviderConfig, items: list[Item], cache: dict, results_path: Path,
                    concurrency: int, fresh: bool) -> dict[str, dict]:
    """item id -> {"answers", "latency_ms"} or {"error"}."""
    label = f"{cfg.provider}:{cfg.model}"
    out: dict[str, dict] = {}
    todo = []
    for item in items:
        hit = cache.get((label, item.key))
        if hit and not fresh and "error" not in hit:
            out[item.id] = hit
        else:
            todo.append(item)
    if not todo:
        return out

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as http:
        provider = make_provider(cfg, http)

        async def one(item: Item):
            async with sem:
                try:
                    res = await provider.ask(item.state, item.questions)
                    rec = {"answers": res.answers, "latency_ms": res.latency_ms}
                except SystemOneError as e:
                    rec = {"error": str(e)}
            rec |= {"model": label, "key": item.key, "id": item.id}
            with results_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            out[item.id] = rec
            done = len(out)
            if done % 10 == 0 or done == len(items):
                print(f"  {label}: {done}/{len(items)}", flush=True)

        await asyncio.gather(*(one(i) for i in todo))
    return out


def score(items: list[Item], results: dict, th: Thresholds) -> dict:
    allows = blocks = kept = false_allow = 0
    wrong = []
    for item in items:
        rec = results.get(item.id)
        if not rec or "error" in rec:
            continue
        verdict = apply_rules(item.site, rec["answers"], th)
        if item.label == "allow":
            allows += 1
            kept += verdict.verdict == "allow"
        else:
            blocks += 1
            false_allow += verdict.verdict == "allow"
        if verdict.verdict != item.label:
            wrong.append((item, verdict))
    return {
        "allows": allows, "blocks": blocks, "kept": kept, "false_allows": false_allow,
        "kept_rate": kept / allows if allows else 0.0,
        "false_allow_rate": false_allow / blocks if blocks else 0.0,
        "wrong": wrong,
    }


def frontier(site: str, items: list[Item], results: dict, base: Thresholds) -> list[tuple[float, dict]]:
    """For each false-allow count up to the gate's budget, the lowest threshold
    that stays within it (keeping the most allows), and its scores."""
    key = SCORE_KEY[site]
    lo = 0.50
    scored = [(t, score(items, results, base.model_copy(update={key: t})))
              for t in (round(lo + i / 100, 2) for i in range(int(round((1 - lo) * 100))))]
    budget = int(GATE["false_allow"] * max(s["blocks"] for _, s in scored))
    points = []
    for k in range(budget + 1):
        ok = [(t, s) for t, s in scored if s["false_allows"] <= k]
        if ok:
            t, s = min(ok, key=lambda ts: ts[0])
            if not points or s["kept"] > points[-1][1]["kept"]:
                points.append((t, s))
    return points


def fmt(s: dict) -> str:
    return (f"kept {s['kept']}/{s['allows']} allows ({s['kept_rate'] * 100:.0f}%), "
            f"false allows {s['false_allows']}/{s['blocks']} ({s['false_allow_rate'] * 100:.1f}%)")


def timeout_for(label: str, config: Config) -> int:
    fb = config.classifier.fallback
    return fb.timeout_ms if fb is not None and fb.label == label else config.classifier.timeout_ms


def report(cfg: ProviderConfig, items: list[Item], results: dict, config: Config, show_errors: bool) -> None:
    errors = [r["error"] for r in results.values() if "error" in r]
    lat = sorted(r["latency_ms"] for r in results.values() if "latency_ms" in r)

    print(f"\n=== {cfg.label} ===")
    if errors:
        print(f"  {len(errors)} errors, e.g. {errors[0][:160]}")
    if not lat:
        return
    pc = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]
    timeout = timeout_for(cfg.label, config)
    fast = pc(0.99) <= timeout
    print(f"  latency p50 {pc(0.5)}ms  p95 {pc(0.95)}ms  p99 {pc(0.99)}ms  max {lat[-1]}ms (includes network); "
          f"daemon timeout {timeout}ms: {'ok' if fast else 'p99 OVER TIMEOUT, would fail closed'}")

    th = config.classifier.thresholds_for(cfg.label)
    for site in sorted({i.site for i in items}):
        site_items = [i for i in items if i.site == site]
        key = SCORE_KEY[site]
        s = score(site_items, results, th)
        print(f"  {site}: {s['allows']} allow / {s['blocks']} block labels")
        print(f"    config  {key} >= {getattr(th, key):.2f}: {fmt(s)}")
        points = frontier(site, site_items, results, th)
        for t, fs in points:
            print(f"    sweep   {key} >= {t:.2f}: {fmt(fs)}")

        enough = min(s["allows"], s["blocks"]) >= GATE["min_labels"]
        best_t, best = points[-1] if points else (None, None)
        if not enough:
            verdict = f"NOT ENOUGH LABELS (need >= {GATE['min_labels']} of each)"
        elif best is not None and best["kept_rate"] >= GATE["kept_allow"] and fast:
            verdict = f"PASS at {key} >= {best_t:.2f}"
        else:
            verdict = "FAIL"
        print(f"    gate (keeps >= {GATE['kept_allow']:.0%} of allows with <= {GATE['false_allow']:.0%} "
              f"false allows, p99 within timeout): {verdict}")

        if show_errors:
            for item, v in sorted(s["wrong"], key=lambda w: w[0].label):
                title = item.state.get("video", {}).get("title") or json.dumps(item.state, ensure_ascii=False)[:100]
                print(f"      labeled {item.label:5} got {v.verdict:5} {item.id}: {title}\n          {v.reason}")


async def main_async(args) -> None:
    config = load_config(args.config)
    rows = labels.load(args.labels)
    rows = {k: r for k, r in rows.items() if r["site"] in SCORE_KEY}
    items, skipped = build_items(rows)
    print(f"{len(items)} labeled items ({len(skipped)} skipped for thin metadata)")
    for s in skipped[:5]:
        print(f"  skipped {s}")
    if not items:
        return

    cache = load_results(args.results)
    for spec in args.models:
        cfg = parse_model(spec)
        results = await run_model(cfg, items, cache, args.results, args.concurrency, args.fresh)
        report(cfg, items, results, config, args.show_errors)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS, metavar="PROVIDER:MODEL[@URL]")
    ap.add_argument("--labels", type=Path, default=DATA / "labels.csv")
    ap.add_argument("--results", type=Path, default=DATA / "results.jsonl")
    ap.add_argument("--config", default=str(REPO / "config.example.yaml"),
                    help="config whose thresholds and timeouts to score against (default: config.example.yaml)")
    ap.add_argument("--env-file", type=Path, help="KEY=VALUE secrets file, e.g. secrets.env")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--fresh", action="store_true", help="ignore cached answers")
    ap.add_argument("--show-errors", action="store_true", help="list misjudged items")
    args = ap.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
