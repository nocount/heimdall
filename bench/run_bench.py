"""Run every labeled item through each decision model and report against your labels.

    uv run --project daemon bench/run_bench.py --env-file /etc/heimdall/secrets.env
    uv run --project daemon bench/run_bench.py --models workers_ai:@cf/cloudflare/clef-flash \\
        local_llamacpp:clef-flash@http://127.0.0.1:8081

Requests are built and verdicts combined by the daemon's own code
(questions.build_request, policy.apply_rules), so the numbers predict the
daemon. Answers are cached in bench/data/results.jsonl; rerunning only calls
the models for new labels. --fresh ignores the cache.

The false-allow rate (blocked-labeled items the model would allow) matters
most: a leak defeats the tool. False blocks are friction, but appealable.
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
from heimdall.config import Config, ProviderConfig, load_config
from heimdall.models.systemone import SystemOneError, make_provider
from heimdall.policy import apply_rules
from heimdall.questions import build_request

DATA = Path(__file__).parent / "data"
DEFAULT_MODELS = [
    "workers_ai:@cf/cloudflare/clef-flash",
    "openrouter:typesafe/jev-1.13",
    "openrouter:microsoft/microsoft-decision-1",
]
GATE = {"accuracy": 0.90, "false_allow": 0.05, "p95_ms": 300}


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


def score(items: list[Item], results: dict, config: Config) -> dict:
    n = correct = blocks = allows = false_allow = false_block = 0
    wrong = []
    for item in items:
        rec = results.get(item.id)
        if not rec or "error" in rec:
            continue
        verdict = apply_rules(item.site, rec["answers"], config)
        n += 1
        if item.label == "block":
            blocks += 1
            false_allow += verdict.verdict == "allow"
        else:
            allows += 1
            false_block += verdict.verdict == "block"
        if verdict.verdict == item.label:
            correct += 1
        else:
            wrong.append((item, verdict))
    return {
        "n": n,
        "accuracy": correct / n if n else 0.0,
        "false_allow": false_allow / blocks if blocks else 0.0,
        "false_block": false_block / allows if allows else 0.0,
        "blocks": blocks, "allows": allows, "wrong": wrong,
    }


def with_thresholds(config: Config, **th) -> Config:
    c = config.model_copy(deep=True)
    for k, v in th.items():
        setattr(c.classifier.thresholds, k, v)
    return c


def frange(lo: float, hi: float, step: float) -> list[float]:
    return [round(lo + i * step, 2) for i in range(int(round((hi - lo) / step)) + 1)]


# Threshold grids per site. The sites' thresholds are independent, so each
# site is swept and gated on its own.
SWEEP_GRIDS = {
    "youtube": [{"educational": e, "content_conf": c}
                for e in frange(0.5, 0.95, 0.05) for c in frange(0.3, 0.9, 0.1)],
    "x": [{"ai_related": a} for a in frange(0.3, 0.95, 0.05)],
}


def sweep(site: str, items: list[Item], results: dict, config: Config) -> list[tuple[dict, dict]]:
    """Score every threshold combination for one site; best first (false-allow within gate, then accuracy)."""
    grid = [(th, score(items, results, with_thresholds(config, **th))) for th in SWEEP_GRIDS[site]]
    grid.sort(key=lambda g: (g[1]["false_allow"] > GATE["false_allow"], -g[1]["accuracy"], g[1]["false_allow"]))
    return grid


def pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def fmt_scores(s: dict) -> str:
    return f"acc {pct(s['accuracy'])}  false-allow {pct(s['false_allow'])}  false-block {pct(s['false_block'])}"


def report(label: str, items: list[Item], results: dict, config: Config, show_errors: bool) -> None:
    errors = [r["error"] for r in results.values() if "error" in r]
    lat = sorted(r["latency_ms"] for r in results.values() if "latency_ms" in r)
    p50 = statistics.median(lat) if lat else None
    p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None

    print(f"\n=== {label} ===")
    if errors:
        print(f"  {len(errors)} errors, e.g. {errors[0][:160]}")
    if not lat:
        return
    fast = p95 < GATE["p95_ms"]
    print(f"  latency p50 {p50:.0f}ms  p95 {p95}ms (includes network)  {'ok' if fast else 'TOO SLOW'}")

    for site in sorted({i.site for i in items}):
        site_items = [i for i in items if i.site == site]
        s = score(site_items, results, config)
        print(f"  {site}: n={s['n']} ({s['allows']} allow / {s['blocks']} block labels)")
        print(f"    at config thresholds:  {fmt_scores(s)}")
        best = sweep(site, site_items, results, config)
        for th, bs in best[:3]:
            knobs = "  ".join(f"{k} {v:.2f}" for k, v in th.items())
            print(f"    sweep {knobs:34} {fmt_scores(bs)}")
        th, bs = best[0]
        ok = bs["accuracy"] >= GATE["accuracy"] and bs["false_allow"] <= GATE["false_allow"] and fast
        print(f"    gate (acc >= 90%, false-allow <= 5%, p95 < 300ms) at best thresholds: {'PASS' if ok else 'FAIL'}")

        if show_errors:
            for item, verdict in s["wrong"]:
                title = item.state.get("video", {}).get("title") or json.dumps(item.state, ensure_ascii=False)[:100]
                print(f"      labeled {item.label:5} got {verdict.verdict:5} {item.id}: {title}\n"
                      f"          {verdict.reason}")


async def main_async(args) -> None:
    config = load_config(args.config) if args.config else Config()
    rows = labels.load(args.labels)
    if args.site:
        rows = {k: r for k, r in rows.items() if r["site"] == args.site}
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
        report(spec, items, results, config, args.show_errors)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS, metavar="PROVIDER:MODEL[@URL]")
    ap.add_argument("--labels", type=Path, default=DATA / "labels.csv")
    ap.add_argument("--results", type=Path, default=DATA / "results.jsonl")
    ap.add_argument("--config", help="config whose thresholds/allow_types to score with (default: built-in)")
    ap.add_argument("--env-file", type=Path, help="KEY=VALUE secrets file, e.g. secrets.env")
    ap.add_argument("--site", choices=["youtube", "x"])
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--fresh", action="store_true", help="ignore cached answers")
    ap.add_argument("--show-errors", action="store_true", help="list misjudged items")
    args = ap.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
