"""What one search cost: candidates, Gemini calls and tokens, build and run time."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def report(run_dir: Path, llm_log: Path | None) -> str:
    recs = lines(run_dir / "records.jsonl")
    status = Counter(r.get("status", "?") for r in recs)
    entries = [c for r in recs for c in r.get("cpp_calls", [])]
    pro = [c for c in entries if "usage" in c or "error" in c]
    tokens = lambda c: (c.get("usage") or {}).get("total_tokens") or 0
    pro_tokens = sum(map(tokens, pro))
    pro_seconds = sum(c.get("seconds") or 0 for c in pro)
    kinds, kind_tokens = Counter(), Counter()
    for c in pro:
        k = re.sub(r"^a\d+_|\d+$", "", c.get("call", "?"))
        kinds[k] += 1
        kind_tokens[k] += tokens(c)
    audits = [r["cpp"]["audit"] for r in recs if isinstance(r.get("cpp"), dict) and r["cpp"].get("audit")]
    kept = [(r["cpp"]["edit_lines"], r["cpp"]["audit"]["lines"]) for r in recs
            if isinstance(r.get("cpp"), dict) and (r["cpp"].get("audit") or {}).get("kept")]
    builds = [c for c in entries if c.get("build_s") is not None]
    build_s = sum(c["build_s"] for c in builds)
    cleaned = sum(1 for c in builds if c.get("cleaned"))
    steps = sorted(r["change_lines"] for r in recs if isinstance(r.get("change_lines"), int))
    stage = Counter()
    for r in recs:
        for k, v in (r.get("seconds") or {}).items():
            stage[k] += v
    run_wall = [max(r["run_seconds"].values()) for r in recs if r.get("run_seconds")]
    ev = lines(llm_log) if llm_log else []
    ev_ok = [e for e in ev if e.get("status") == 200]
    ev_tokens = sum(e.get("total_tokens") or 0 for e in ev_ok)
    ev_seconds = sum(e.get("seconds") or 0 for e in ev)

    out = [f"{'candidates':<28}{len(recs):>10}   " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())),
           f"{'evolver calls (Gemini)':<28}{len(ev):>10}   {len(ev) - len(ev_ok)} failed, {ev_tokens:,} tokens, {ev_seconds:,.0f} s",
           f"{'C++ calls (Pro)':<28}{len(pro):>10}   {pro_tokens:,} tokens, {pro_seconds:,.0f} s; "
           + ", ".join(f"{k} {n} ({kind_tokens[k]:,} tokens)" for k, n in sorted(kinds.items())),
           f"{'drift audits':<28}{len(audits):>10}   {len(kept)} kept"
           + (": code lines changed " + ", ".join(f"{b}→{a}" for b, a in kept) if kept else ""),
           f"{'builds':<28}{len(builds):>10}   {build_s:,.0f} s, {cleaned} clean rebuilds",
           f"{'step size (canonical lines)':<28}{len(steps):>10}   "
           + (f"median {steps[len(steps) // 2]}, max {steps[-1]}" if steps else "none recorded"),
           f"{'simulation wall per candidate':<28}{'':>10}   "
           + (f"{min(run_wall):,.0f}–{max(run_wall):,.0f} s over {len(run_wall)} scored" if run_wall else "none finished"),
           f"{'evaluator seconds by stage':<28}{'':>10}   " + ", ".join(f"{k} {v:,.0f}" for k, v in sorted(stage.items())),
           "",
           f"cost log line: {len(recs)} candidates ({', '.join(f'{v} {k}' for k, v in sorted(status.items()))}); "
           f"evolver {len(ev)} calls, {ev_tokens:,} tokens; Pro {len(pro)} calls, {pro_tokens:,} tokens"
           f" ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}); "
           f"builds {build_s:,.0f} s ({cleaned} clean); evaluator {stage.get('total', 0):,.0f} s in all"]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", type=Path, required=True, help="the yaml evaluator's A3_RUN_DIR")
    ap.add_argument("--llm-log", type=Path, default=None, help="the driver's llm_calls.jsonl")
    args = ap.parse_args()
    print(report(args.run_dir, args.llm_log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
