"""An LLC replacement-policy evolution loop on one machine (CHIA hackathon)."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHAMPSIM = Path(os.environ.get("CHAMPSIM_ROOT") or Path.home() / "champsim")
sys.path.insert(0, str(CHAMPSIM))

from chia_node.champsim import build_replacement, run_simulation
from feedback.area_energy import (DEFAULT_NODE_NM, CactiError, energy,
                                  parse_json_run, price_policy)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate import MODULE, seed_sources

TRACES = Path.home() / "traces"
POLICIES = CHAMPSIM / "structure_description" / "policies"
VALIDATOR = CHAMPSIM / "structure_description" / "validate.py"
WEIGHTS = CHAMPSIM / "weights.txt"
SELECTED = CHAMPSIM / "selected_traces.txt"
BASELINE_JSON = Path.home() / "loop_out" / "lru_baseline.json"

def load_selected() -> tuple[list[str], list[str]]:
    training, held_out = [], []
    if not SELECTED.exists():
        return training, held_out
    for line in SELECTED.read_text().splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        stem, group = fields[0], fields[1]
        (training if group == "training" else held_out).append(stem)
    return training, held_out


EXPLORE, VALIDATE = load_selected()

EXPLORE_WARMUP, EXPLORE_SIM = 20_000_000, 50_000_000
VALIDATE_WARMUP, VALIDATE_SIM = 50_000_000, 100_000_000
FINAL_WARMUP, FINAL_SIM = 50_000_000, 200_000_000

WARMUP, SIM = EXPLORE_WARMUP, EXPLORE_SIM
OPT_SETS = 256
WORKERS = int(os.environ.get("CHIA_WORKERS") or max(1, (os.cpu_count() or 4) - 2))


def trace_path(stem: str) -> str:
    return str(TRACES / f"{stem}.champsimtrace.xz")


def absent(group: list[str]) -> list[str]:
    return [s for s in group if not Path(trace_path(s)).exists()]


def geomean(xs) -> float:
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else 0.0


def structure_description_is_legal(structure_description_yaml: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(structure_description_yaml)
        path = fh.name
    try:
        done = subprocess.run([sys.executable, str(VALIDATOR), path],
                              capture_output=True, text=True, timeout=60)
        return done.returncode == 0, (done.stdout + done.stderr).strip()
    finally:
        Path(path).unlink(missing_ok=True)


SIM_TIMEOUT_S = 10_800


def run_traces(binary: bytes, group: list[str], *, opt_log_dir: Path | None = None,
               tag: str = "", warmup: int = WARMUP, sim: int = SIM,
               timeout_s: int = SIM_TIMEOUT_S) -> dict:
    missing = absent(group)
    if missing:
        raise FileNotFoundError(f"{len(missing)} trace(s) not on this machine: "
                                f"{', '.join(missing)}")

    def one(stem: str):
        opt = ({"opt_log": str(opt_log_dir / f"{tag}.{stem}.opt.log"),
                "opt_sets": OPT_SETS} if opt_log_dir else {})
        return stem, run_simulation(binary, trace_path(stem), warmup_instructions=warmup,
                                    simulation_instructions=sim, timeout_s=timeout_s, **opt)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(pool.map(one, group))


def run_group(binary: bytes, group: list[str], **kw) -> dict[str, float | None]:
    return {stem: (run.ipc if run.success else None)
            for stem, run in run_traces(binary, group, **kw).items()}


def cacti_metrics(structure_description_yaml: str, config: dict, runs: dict, words=None) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(structure_description_yaml)
        path = fh.name
    try:
        llc = config["LLC"]
        priced = price_policy(path, llc["sets"], llc["ways"], config["num_cores"],
                              DEFAULT_NODE_NM, words=words)
    finally:
        Path(path).unlink(missing_ok=True)
    hz = config["ooo_cpu"][0]["frequency"] * 1e6
    energy_nj = 0.0
    for run in runs.values():
        stats = parse_json_run(run.stats)
        seconds = stats.cycles / hz
        energy_nj += sum(energy(a, stats.accesses["LLC"], seconds)["energy_nj"]
                         for a in priced["arrays"])
    return {"metadata_kb": priced["sram_bits"] / 8192,
            "metadata_flipflop_bits": priced["register_bits"],
            "metadata_bits_per_line": priced["array_bits_per_line"],
            "metadata_area_mm2": priced["area_um2"] / 1e6,
            "metadata_energy_uj": energy_nj / 1e3,
            "cacti_node_nm": DEFAULT_NODE_NM}


def build(module: str, sources: dict[str, str]):
    return build_replacement(str(CHAMPSIM), module, sources)


BASELINE_MODULE = "basis_policy"


def renamed_lru(module: str) -> dict[str, str]:
    out = {}
    for name in ("lru.h", "lru.cc"):
        text = (CHAMPSIM / "replacement" / "lru" / name).read_text()
        text = text.replace("REPLACEMENT_LRU_H", f"REPLACEMENT_{module.upper()}_H")
        text = text.replace('"lru.h"', f'"{module}.h"').replace("lru", module)
        out[name.replace("lru", module)] = text
    return out


def measure_baseline() -> dict:
    print(f"measuring the LRU baseline on all {len(EXPLORE) + len(VALIDATE)} traces "
          f"({WORKERS} at a time)")
    built = build(BASELINE_MODULE, renamed_lru(BASELINE_MODULE))
    if not built.success:
        print(f"  build failed:\n{built.build_diagnostics[-2000:]}")
        raise SystemExit(1)
    t0 = time.time()
    ipcs = run_group(built.binary, EXPLORE + VALIDATE, tag="lru")
    BASELINE_JSON.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_JSON.write_text(json.dumps(ipcs, indent=2))
    print(f"  done in {time.time() - t0:.0f}s -> {BASELINE_JSON}")
    for stem, ipc in ipcs.items():
        print(f"    {stem:28s} {ipc if ipc is None else f'{ipc:.4f}'}")
    return ipcs


def load_baseline() -> dict:
    if not BASELINE_JSON.exists():
        print(f"no baseline at {BASELINE_JSON}; run with --baseline first")
        raise SystemExit(1)
    return json.loads(BASELINE_JSON.read_text())


def benchmark_of(stem: str) -> str:
    return re.sub(r"-\d+B$", "", stem)


def load_weights() -> dict[str, float]:
    out: dict[str, float] = {}
    if not WEIGHTS.exists():
        return out
    for line in WEIGHTS.read_text().splitlines():
        name, _, value = line.partition(":")
        if value.strip():
            out[name.strip()] = float(value)
    return out


def score(ipcs: dict, baseline: dict,
          weights: dict | None = None) -> tuple[float, dict, dict]:
    rel = {}
    for stem, ipc in ipcs.items():
        base = baseline.get(stem)
        rel[stem] = (ipc / base) if (ipc and base) else None

    weights = load_weights() if weights is None else weights
    slices: dict[str, list[tuple[float, float, float]]] = {}
    for stem, ipc in ipcs.items():
        base = baseline.get(stem)
        if ipc and base:
            slices.setdefault(benchmark_of(stem), []).append(
                (ipc, base, weights.get(stem, 1.0)))

    per_benchmark = {}
    for bench, items in slices.items():
        total = sum(w for _, _, w in items)
        if total <= 0:
            continue
        policy_ipc = total / sum(w / ipc for ipc, _, w in items if ipc > 0)
        lru_ipc = total / sum(w / base for _, base, w in items if base > 0)
        per_benchmark[bench] = policy_ipc / lru_ipc
    return geomean(per_benchmark.values()), rel, per_benchmark


def evaluate(module: str, sources: dict[str, str], group: list[str],
             baseline: dict) -> dict:
    built = build(module, sources)
    if not built.success:
        return {"status": "build_error", "score": 0.0,
                "diagnostics": built.build_diagnostics[-4000:]}
    ipcs = run_group(built.binary, group, tag=module)
    s, rel, per_benchmark = score(ipcs, baseline)
    llc = built.config["LLC"]
    return {"status": "ok" if any(rel.values()) else "all_runs_failed", "score": s,
            "ipc": ipcs, "relative": rel, "benchmark": per_benchmark,
            "llc": f"{llc['sets']}x{llc['ways']}", "build_s": built.build_duration_s}


def show(label: str, res: dict) -> None:
    print(f"  {label}: {res['status']}  geomean IPC vs LRU {res['score']:.4f}"
          f"{'  LLC ' + res['llc'] if res.get('llc') else ''}")
    for stem, r in (res.get("relative") or {}).items():
        ipc = (res.get("ipc") or {}).get(stem)
        print(f"    {stem:28s} IPC {ipc if ipc is None else f'{ipc:.4f}'}"
              f"   x{'--' if r is None else f'{r:.4f}'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=2)
    ap.add_argument("--seed", default="mockingjay")
    ap.add_argument("--baseline", action="store_true", help="measure LRU and exit")
    ap.add_argument("--dry-run", action="store_true", help="score the seed, no model call")
    args = ap.parse_args()

    if args.baseline:
        measure_baseline()
        return 0

    baseline = load_baseline()
    structure_description = (POLICIES / f"{args.seed}.yaml").read_text()
    sources = seed_sources(args.seed)
    legal, why = structure_description_is_legal(structure_description)
    print(f"seed {args.seed}: schema {'ok' if legal else 'REJECTED'}  {why}")

    missing = absent(EXPLORE)
    if missing:
        print(f"{len(missing)} training trace(s) not on this machine: {', '.join(missing)}")
        return 1

    print(f"\nexplore group, {len(EXPLORE)} traces, {WORKERS} at a time")
    t0 = time.time()
    best = evaluate(MODULE, sources, EXPLORE, baseline)
    show("seed", best)
    print(f"  ({time.time() - t0:.0f}s)")

    if not args.dry_run:
        print("\nAlphaEvolve wiring is not in this file yet; use --dry-run")

    held = None
    missing = absent(VALIDATE)
    if missing:
        print(f"\nheld-out group: {len(missing)} of {len(VALIDATE)} traces not on this "
              f"machine, validation not run: {', '.join(missing)}")
    else:
        print(f"\nheld-out group, {len(VALIDATE)} traces")
        held = evaluate(MODULE, sources, VALIDATE, baseline)
        show("held-out", held)
        print(f"\n  explore  {best['score']:.4f}   held-out {held['score']:.4f}"
              f"   gap {best['score'] - held['score']:+.4f}")

    out = Path.home() / "loop_out" / "result.json"
    out.write_text(json.dumps({"explore": best, "validate": held}, indent=2, default=str))
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
