"""Find which traces can tell two replacement policies apart (CHIA hackathon)."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHAMPSIM = Path.home() / "champsim"
sys.path.insert(0, str(CHAMPSIM))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia_llc_loop import (BASELINE_MODULE, EXPLORE, VALIDATE, build,
                          renamed_lru, trace_path)
from candidate import MODULE, seed_sources
from chia_node.champsim import run_simulation


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--warmup", type=int, default=20_000_000)
    ap.add_argument("--sim", type=int, default=50_000_000)
    ap.add_argument("--seed", default="mockingjay")
    ap.add_argument("--workers", type=int, default=14,
                    help="concurrent simulations; leave a core or two free")
    ap.add_argument("--traces", default="all", choices=("all", "explore"))
    ap.add_argument("--trace-file", type=Path,
                    help="a file of trace stems, one per line, e.g. the DPC4 "
                         "repo's selected_traces.txt; overrides --traces")
    ap.add_argument("--set", choices=("training", "held-out"),
                    help="keep only traces whose second column is this set")
    ap.add_argument("--out", type=Path,
                    default=Path.home() / "loop_out" / "fidelity_probe.json")
    args = ap.parse_args()

    if args.trace_file:
        traces = []
        for line in args.trace_file.read_text().splitlines():
            fields = line.split("#", 1)[0].split()
            if not fields:
                continue
            if args.set and (len(fields) < 2 or fields[1] != args.set):
                continue
            traces.append(fields[0])
    else:
        traces = EXPLORE if args.traces == "explore" else EXPLORE + VALIDATE

    absent = [t for t in traces if not Path(trace_path(t)).exists()]
    if absent:
        print(f"{len(absent)} of {len(traces)} traces are not downloaded, skipping:")
        for t in absent[:10]:
            print(f"  {t}")
        if len(absent) > 10:
            print(f"  ... and {len(absent) - 10} more")
        traces = [t for t in traces if t not in set(absent)]
    if not traces:
        print("no traces to run")
        return 1

    runs = len(traces) * 2
    waves = -(-runs // args.workers)
    print(f"{len(traces)} traces x 2 policies = {runs} simulations at "
          f"{args.warmup / 1e6:.0f}M warmup / {args.sim / 1e6:.0f}M sim, "
          f"{args.workers} at a time ({waves} waves)")

    print("building...")
    lru = build(BASELINE_MODULE, renamed_lru(BASELINE_MODULE))
    pol = build(MODULE, seed_sources(args.seed))
    for name, b in ((BASELINE_MODULE, lru), (args.seed, pol)):
        if not b.success:
            print(f"  {name} failed to build:\n{b.build_diagnostics[-2000:]}")
            return 1
    print(f"  ok, LLC {lru.config['LLC']['sets']}x{lru.config['LLC']['ways']}")

    jobs = [(tag, binary, stem)
            for tag, binary in (("lru", lru.binary), ("policy", pol.binary))
            for stem in traces]

    def one(job):
        tag, binary, stem = job
        run = run_simulation(binary, trace_path(stem), warmup_instructions=args.warmup,
                             simulation_instructions=args.sim)
        return tag, stem, (run.ipc if run.success else None)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, jobs))
    print(f"  {len(jobs)} simulations in {time.time() - t0:.0f}s")

    ipc = {"lru": {}, "policy": {}}
    for tag, stem, val in results:
        ipc[tag][stem] = val

    rows = []
    for stem in traces:
        a, b = ipc["lru"].get(stem), ipc["policy"].get(stem)
        rel = (b / a) if (a and b) else None
        rows.append((stem, a, b, rel))
    rows.sort(key=lambda r: abs((r[3] or 1.0) - 1.0), reverse=True)

    print(f"\n{'trace':28s} {'LRU IPC':>9s} {'policy':>9s} {'ratio':>9s}  benchmark")
    for stem, a, b, rel in rows:
        fmt = lambda v: "  --" if v is None else f"{v:9.4f}"
        flag = "" if rel is None or abs(rel - 1.0) >= 0.002 else "   <- flat"
        print(f"{stem:28s} {fmt(a)} {fmt(b)} {fmt(rel)}  "
              f"{re.sub(r'-[0-9]+B$', '', stem)}{flag}")

    live = [r for r in rows if r[3] is not None and abs(r[3] - 1.0) >= 0.002]
    print(f"\n{len(live)} of {len(rows)} traces separate the policies by >=0.2%")
    print("those are the ones worth simulating per candidate:")
    print("  " + ", ".join(f'"{r[0]}"' for r in live))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"warmup": args.warmup, "sim": args.sim, "ipc": ipc,
         "ratio": {r[0]: r[3] for r in rows}}, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
