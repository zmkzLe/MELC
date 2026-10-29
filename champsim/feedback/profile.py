"""Profile of one candidate run: where its cycles went, and what each block cost."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from feedback.area_energy import DEFAULT_NODE_NM, PRICES_DIR, CACTI_BIN, feedback as area_energy

PHASE = "=== Simulation ==="
LEVELS = ("L1I", "L1D", "L2C", "LLC")
CAUSES = ("retiring", "frontend", "speculation", "backend_core", "backend_memory")
MEMORY_LEVELS = ("lsq", "L1D", "L2C", "LLC", "DRAM", "deeper", "unknown", "in_flight")

_RUNS = re.compile(r"^CPU (\d+) runs (\S+)", re.M)
_CPU = re.compile(r"^CPU (\d+) cumulative IPC: \S+ instructions: (\d+) cycles: (\d+)", re.M)
_BRANCH = re.compile(r"^CPU (\d+) Branch Prediction Accuracy: (\S+)% MPKI: (\S+) Average ROB Occupancy at Mispredict: (\S+)", re.M)
_CAUSE = re.compile(r"^CPU (\d+) CYCLES BY CAUSE retiring: (\d+) frontend: (\d+) speculation: (\d+) backend_core: (\d+) "
                    r"backend_memory: (\d+) residual: (-?\d+)", re.M)
_MEMORY = re.compile(r"^CPU (\d+) BACKEND MEMORY CYCLES lsq: (\d+) L1D: (\d+) L2C: (\d+) LLC: (\d+) DRAM: (\d+) "
                     r"deeper: (\d+) unknown: (\d+) in_flight: (-?\d+)", re.M)
_ACCESS = re.compile(r"^cpu(\d+)->(?:cpu\d+_)?(\w+) (TOTAL|LOAD|RFO|PREFETCH|WRITE|TRANSLATION)\s+ACCESS:\s+(\d+) HIT:\s+(\d+) "
                     r"MISS:\s+(\d+) MSHR_MERGE:\s+(\d+)", re.M)
_PREFETCH = re.compile(r"^cpu(\d+)->(?:cpu\d+_)?(\w+) PREFETCH REQUESTED:\s+(\d+) ISSUED:\s+(\d+) USEFUL:\s+(\d+) USELESS:\s+(\d+)", re.M)
_LATENCY = re.compile(r"^cpu(\d+)->(?:cpu\d+_)?(\w+) AVERAGE MISS LATENCY: (\S+) cycles", re.M)
_MSHR = re.compile(r"^(?:cpu\d+_)?(\w+) MSHR OCCUPANCY average: (\S+) max: (\d+) size: (\d+)", re.M)
_DRAM_RQ = re.compile(r"^Channel (\d+) RQ ROW_BUFFER_HIT:\s+(\d+)\n\s+ROW_BUFFER_MISS:\s+(\d+)\n\s+AVG DBUS CONGESTED CYCLE: (\S+)", re.M)
_DRAM_WQ = re.compile(r"^Channel (\d+) WQ ROW_BUFFER_HIT:\s+(\d+)\n\s+ROW_BUFFER_MISS:\s+(\d+)\n\s+FULL:\s+(\d+)", re.M)


def _num(s: str) -> float | None:
    return None if s == "-" else float(s)


_CPU_PREFIX = re.compile(r"^cpu\d+_")
JSON_ABSENT = ("branch_accuracy_pct", "branch_mpki", "wq_full")


def parse_json(data: list) -> dict:
    from feedback.area_energy import ACCESS_TYPES, sim_phase

    phase = sim_phase(data)
    roi = phase["roi"]
    traces = {i: t for i, t in enumerate(phase.get("traces", []))}

    cpus = {}
    for i, core in enumerate(roi["cores"]):
        cause = dict(core["cycles by cause"])
        cpus[i] = {
            "instructions": core["instructions"],
            "cycles": core["cycles"],
            "branch_accuracy_pct": None,
            "branch_mpki": None,
            "rob_at_mispredict": core.get("Avg ROB occupancy at mispredict"),
            "cycles_by_cause": {c: int(cause.get(c, 0)) for c in CAUSES} | {"residual": int(cause.get("residual", 0))},
            "backend_memory_cycles": {k: int(core["backend memory cycles"].get(k, 0)) for k in MEMORY_LEVELS},
        }
    if not cpus:
        raise ValueError("JSON output has no cores")

    caches: dict[str, dict] = {}
    for name, blk in roi.items():
        if name in ("cores", "DRAM") or not isinstance(blk, dict):
            continue
        d = caches.setdefault(_CPU_PREFIX.sub("", name), {"by_type": {}})
        for typ in ACCESS_TYPES:
            e = blk.get(typ)
            if not isinstance(e, dict):
                continue
            hits, misses = sum(e.get("hit", [])), sum(e.get("miss", []))
            merges = sum(e.get("mshr_merge", []))
            entry = {"accesses": hits + misses, "hits": hits, "misses": misses, "mshr_merges": merges}
            t = d["by_type"].setdefault(typ, {k: 0 for k in entry})
            for k, v in entry.items():
                t[k] += v
                d[k] = d.get(k, 0) + v
        pf = d.setdefault("prefetch", {"requested": 0, "issued": 0, "useful": 0, "useless": 0})
        for key, field in (("requested", "prefetch requested"), ("issued", "prefetch issued"),
                           ("useful", "useful prefetch"), ("useless", "useless prefetch")):
            pf[key] += int(blk.get(field, 0))
        if blk.get("miss latency") is not None:
            d["avg_miss_latency"] = float(blk["miss latency"])
        occ = blk.get("mshr occupancy")
        if isinstance(occ, dict):
            d.update(mshr_avg=float(occ["average"]), mshr_max=int(occ["max"]), mshr_size=int(occ["size"]))

    dram = {}
    for ch, entry in enumerate(roi.get("DRAM", [])):
        dram[ch] = {
            "rq_row_buffer_hits": int(entry.get("RQ ROW_BUFFER_HIT", 0)),
            "rq_row_buffer_misses": int(entry.get("RQ ROW_BUFFER_MISS", 0)),
            "avg_dbus_congested_cycles": entry.get("AVG DBUS CONGESTED CYCLE"),
            "wq_row_buffer_hits": int(entry.get("WQ ROW_BUFFER_HIT", 0)),
            "wq_row_buffer_misses": int(entry.get("WQ ROW_BUFFER_MISS", 0)),
            "wq_full": None,
        }
    return {"traces": traces, "cpus": cpus, "caches": caches, "dram": dram}


def parse_log(log_path: str | Path) -> dict:
    text = Path(log_path).read_text()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        return parse_json(data)
    if PHASE not in text:
        raise ValueError(f"{log_path}: no '{PHASE}' block; the run did not finish")
    block = text.split(PHASE, 1)[1]

    cpus = {}
    for c, i, cy in _CPU.findall(block):
        cpus[int(c)] = {"instructions": int(i), "cycles": int(cy)}
    if not cpus:
        raise ValueError(f"{log_path}: no 'CPU n cumulative IPC' line")
    for c, acc, mpki, rob in _BRANCH.findall(block):
        cpus[int(c)].update(branch_accuracy_pct=_num(acc), branch_mpki=_num(mpki), rob_at_mispredict=_num(rob))
    for m in _CAUSE.findall(block):
        cpus[int(m[0])]["cycles_by_cause"] = dict(zip(CAUSES, map(int, m[1:6])), residual=int(m[6]))
    for m in _MEMORY.findall(block):
        cpus[int(m[0])]["backend_memory_cycles"] = dict(zip(MEMORY_LEVELS, map(int, m[1:])))
    for c in cpus:
        for key in ("cycles_by_cause", "backend_memory_cycles"):
            if key not in cpus[c]:
                raise ValueError(f"{log_path}: CPU {c} has no {key.replace('_', ' ')} line; "
                                 "the log was not written by the profiling DPC4-ChampSim")

    caches: dict[str, dict] = {}
    for c, name, typ, acc, hit, miss, merge in _ACCESS.findall(block):
        d = caches.setdefault(name, {"by_type": {}})
        entry = {"accesses": int(acc), "hits": int(hit), "misses": int(miss), "mshr_merges": int(merge)}
        if typ == "TOTAL":
            for k, v in entry.items():
                d[k] = d.get(k, 0) + v
        else:
            t = d["by_type"].setdefault(typ, {k: 0 for k in entry})
            for k, v in entry.items():
                t[k] += v
    for c, name, req, iss, useful, useless in _PREFETCH.findall(block):
        d = caches.setdefault(name, {"by_type": {}})
        pf = d.setdefault("prefetch", {"requested": 0, "issued": 0, "useful": 0, "useless": 0})
        for k, v in zip(("requested", "issued", "useful", "useless"), (req, iss, useful, useless)):
            pf[k] += int(v)
    for c, name, lat in _LATENCY.findall(block):
        caches.setdefault(name, {"by_type": {}})["avg_miss_latency"] = _num(lat)
    for name, avg, mx, size in _MSHR.findall(block):
        caches.setdefault(name, {"by_type": {}}).update(mshr_avg=_num(avg), mshr_max=int(mx), mshr_size=int(size))

    dram = {}
    for ch, hit, miss, cong in _DRAM_RQ.findall(block):
        dram.setdefault(int(ch), {}).update(rq_row_buffer_hits=int(hit), rq_row_buffer_misses=int(miss),
                                            avg_dbus_congested_cycles=_num(cong))
    for ch, hit, miss, full in _DRAM_WQ.findall(block):
        dram.setdefault(int(ch), {}).update(wq_row_buffer_hits=int(hit), wq_row_buffer_misses=int(miss), wq_full=int(full))

    return {"traces": {int(c): t for c, t in _RUNS.findall(block)}, "cpus": cpus, "caches": caches, "dram": dram}


def _frac(n: float, d: float) -> float:
    return n / d if d else 0.0


def _block(name: str, cache: dict, instructions: int, ae_level: dict | None) -> dict:
    misses = cache.get("misses", 0)
    out = {
        "accesses": cache.get("accesses", 0),
        "misses": misses,
        "mpki": 1000.0 * misses / instructions,
        "avg_miss_latency": cache.get("avg_miss_latency"),
        "mshr_occupancy": _frac(cache["mshr_avg"] or 0.0, cache["mshr_size"]) if "mshr_size" in cache else None,
        "mshr_max": cache.get("mshr_max"),
        "mshr_size": cache.get("mshr_size"),
        "area_mm2": ae_level["area_um2"] / 1e6 if ae_level else None,
        "energy_mj": ae_level["energy_nj"] / 1e6 if ae_level else None,
    }
    return out


def profile(config_path: str | Path, policy_path: str | Path, log_path: str | Path, parent: str | None = None,
            node_nm: int = DEFAULT_NODE_NM, prices_dir: Path = PRICES_DIR, cacti: Path = CACTI_BIN,
            words=None) -> dict:
    raw = parse_log(log_path)
    ae = area_energy(config_path, policy_path, log_path, node_nm, prices_dir, cacti, words)
    config = json.loads(Path(config_path).read_text())

    cpu0 = raw["cpus"][0]
    cycles, instructions = cpu0["cycles"], cpu0["instructions"]
    cause = cpu0["cycles_by_cause"]
    mem = cpu0["backend_memory_cycles"]
    accounted = sum(cause[c] for c in CAUSES)
    split = sum(mem.values())

    cycles_by_cause = {c: _frac(cause[c], cycles) for c in CAUSES}
    cycles_by_cause["_residual"] = _frac(cycles - accounted, cycles)
    memory_by_level = {k.lower(): _frac(mem[k], cycles) for k in MEMORY_LEVELS}

    blocks = {}
    for name in LEVELS:
        if name not in raw["caches"]:
            raise ValueError(f"{log_path}: no statistics for {name}")
        blocks[name] = _block(name, raw["caches"][name], instructions, ae["levels"].get(name))
    blocks["BTB"] = {
        "accesses": None,
        "mispredict_mpki": cpu0.get("branch_mpki"),
        "branch_accuracy_pct": cpu0.get("branch_accuracy_pct"),
        "rob_occupancy_at_mispredict": cpu0.get("rob_at_mispredict"),
        "area_mm2": None,
        "energy_mj": None,
    }
    meta = ae["policy_metadata"]
    blocks["policy_metadata"] = {
        "bits_per_line": meta["array_bits_per_line"],
        "bits_per_line_incl_registers": meta["bits_per_line"],
        "tables": len(meta["arrays"]),
        "sram_kb": meta["sram_bits"] / 8192,
        "register_bits": meta["register_bits"],
        "area_mm2": meta["area_um2"] / 1e6,
        "energy_mj": meta["energy_nj"] / 1e6,
    }

    per_core = []
    for c, cpu in sorted(raw["cpus"].items()):
        cc = cpu["cycles_by_cause"]
        per_core.append({"core": c, "ipc": _frac(cpu["instructions"], cpu["cycles"]),
                         **{k: _frac(cc[k], cpu["cycles"]) for k in CAUSES},
                         "llc_occupancy": None, "solo_ipc": None, "interference": None})

    prefetch = {}
    for name in LEVELS:
        pf = raw["caches"][name].get("prefetch", {})
        prefetch[name] = {**pf, "accuracy": _frac(pf.get("useful", 0), pf.get("issued", 0)) if pf.get("issued") else None}

    energy_parts = [lv["energy_nj"] for lv in ae["levels"].values()] + [meta["energy_nj"]]
    checks = {
        "cycle_residual": cycles_by_cause["_residual"],
        "memory_split_residual": _frac(cause["backend_memory"] - split, cycles),
        "energy_residual": _frac(ae["total"]["energy_nj"] - sum(energy_parts), ae["total"]["energy_nj"]),
    }

    return {
        "run_id": Path(log_path).stem,
        "source": "champsim",
        "config": config["executable_name"],
        "workload": Path(raw["traces"].get(0, "")).name.split(".champsimtrace")[0] or None,
        "candidate": meta["policy"],
        "parent": parent,
        "totals": {"cycles": cycles, "instructions": instructions, "ipc": _frac(instructions, cycles)},
        "cycles_by_cause": cycles_by_cause,
        "backend_memory_by_level": memory_by_level,
        "blocks": blocks,
        "per_core": per_core,
        "prefetch": prefetch,
        "dram": raw["dram"],
        "checks": checks,
        "node_nm": node_nm,
    }


def report(p: dict) -> str:
    t = p["totals"]
    lines = [f"{p['run_id']}  {p['candidate']} on {p['workload']}  IPC {t['ipc']:.4f}  ({t['instructions']} instr, {t['cycles']} cycles)",
             "  cycles by cause: " + "  ".join(f"{k} {v:.3f}" for k, v in p["cycles_by_cause"].items()),
             "  backend memory:  " + "  ".join(f"{k} {v:.3f}" for k, v in p["backend_memory_by_level"].items() if v),
             f"  {'block':<16}{'accesses':>12}{'MPKI':>9}{'MSHR occ':>10}{'area mm2':>10}{'energy mJ':>11}"]
    for name in LEVELS:
        b = p["blocks"][name]
        occ = "-" if b["mshr_occupancy"] is None else f"{b['mshr_occupancy']:.3f}"
        lines.append(f"  {name:<16}{b['accesses']:>12}{b['mpki']:>9.3f}{occ:>10}{b['area_mm2']:>10.4f}{b['energy_mj']:>11.4f}")
    m = p["blocks"]["policy_metadata"]
    lines.append(f"  {'metadata':<16}{'':>12}{'':>9}{'':>10}{m['area_mm2']:>10.4f}{m['energy_mj']:>11.4f}"
                 f"   {m['bits_per_line']:.2f} bits/line, {m['tables']} table(s)")
    b = p["blocks"]["BTB"]
    lines.append(f"  BTB mispredict MPKI {b['mispredict_mpki']}   checks: " + "  ".join(f"{k} {v:.2e}" for k, v in p["checks"].items()))
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help="ChampSim config JSON")
    ap.add_argument("--policy", required=True, help="structure description YAML")
    ap.add_argument("--log", required=True, help="ChampSim output of the candidate's run")
    ap.add_argument("--parent", default=None, help="parent candidate id, if known")
    ap.add_argument("--node", type=int, default=DEFAULT_NODE_NM, help="CACTI process node in nm")
    ap.add_argument("--prices", type=Path, default=PRICES_DIR)
    ap.add_argument("--cacti", type=Path, default=CACTI_BIN)
    ap.add_argument("--out", type=Path, default=None, help="write the JSON here as well")
    ap.add_argument("--report", action="store_true", help="print a summary instead of JSON")
    args = ap.parse_args()
    p = profile(args.config, args.policy, args.log, args.parent, args.node, args.prices, args.cacti)
    if args.out:
        args.out.write_text(json.dumps(p, indent=1) + "\n")
    print(report(p) if args.report else json.dumps(p, indent=1))
