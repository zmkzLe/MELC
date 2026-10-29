"""CACTI area and energy for one candidate run on a ChampSim config."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from structure_description.cacti import budget as metadata_budget
from structure_description.validate import load as load_description

CACTI_BIN = ROOT / "tools" / "cacti" / "cacti"
PRICES_DIR = Path(__file__).resolve().parent / "prices"
DEFAULT_NODE_NM = 22
LEVELS = ("L1I", "L1D", "L2C", "LLC")

CACTI_ADDRESS_BITS = 42
CACTI_EXTRA_TAG_BITS = 5


class CactiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Priced:
    depth: int
    width_bits: int
    block_bytes: int
    size_bytes: int
    node_nm: int
    area_um2: float
    read_energy_nj: float
    write_energy_nj: float
    leakage_mw: float
    access_ns: float


@dataclass(frozen=True)
class Level:
    name: str
    sets: int
    ways: int
    block_bytes: int

    @property
    def data_depth(self) -> int:
        return self.sets * self.ways

    @property
    def data_width_bits(self) -> int:
        return self.block_bytes * 8

    @property
    def tag_depth(self) -> int:
        return self.sets

    @property
    def tag_width_bits(self) -> int:
        return self.ways * tag_bits(self.block_bytes)


@dataclass(frozen=True)
class RunStats:
    instructions: int
    cycles: int
    accesses: dict


def tag_bits(block_bytes: int) -> int:
    t = CACTI_ADDRESS_BITS + CACTI_EXTRA_TAG_BITS - int(math.log2(block_bytes))
    return (t + 3) // 4 * 4


def cacti_cfg(size_bytes: int, block_bytes: int, node_nm: int) -> str:
    return f"""\
-size (bytes) {size_bytes}
-block size (bytes) {block_bytes}
-associativity 1
-read-write port 1
-exclusive read port 0
-exclusive write port 0
-single ended read ports 0
-UCA bank count 1
-technology (u) {node_nm / 1000:.3f}
-page size (bits) 8192
-burst length 8
-internal prefetch width 8
-Data array cell type - "itrs-hp"
-Data array peripheral type - "itrs-hp"
-Tag array cell type - "itrs-hp"
-Tag array peripheral type - "itrs-hp"
-output/input bus width {block_bytes * 8}
-operating temperature (K) 360
-cache type "ram"
-tag size (b) "default"
-access mode (normal, sequential, fast) - "normal"
-design objective (weight delay, dynamic power, leakage power, cycle time, area) 0:0:0:0:100
-deviate (delay, dynamic power, leakage power, cycle time, area) 100000:100000:100000:100000:100000
-Optimize ED or ED^2 (ED, ED^2, NONE): "NONE"
-Cache model (NUCA, UCA)  - "UCA"
-NUCA bank count 0
-Wire signaling (fullswing, lowswing, default) - "Global_30"
-Wire inside mat - "semi-global"
-Wire outside mat - "semi-global"
-Interconnect projection - "conservative"
-Core count 8
-Cache level (L2/L3) - "L3"
-Add ECC - "false"
-Print level (DETAILED, CONCISE) - "CONCISE"
-Print input parameters - "false"
-Force cache config - "false"
-Ndwl 1
-Ndbl 1
-Nspd 0
-Ndcm 1
-Ndsam1 0
-Ndsam2 0
-dram_type "DDR3"
-io state "WRITE"
-addr_timing 1.0
-mem_density 4 Gb
-bus_freq 800 MHz
-duty_cycle 1.0
-activity_dq 1.0
-activity_ca 0.5
-num_dq 72
-num_dqs 18
-num_ca 25
-num_clk 2
-num_mem_dq 2
-mem_data_width 8
-rtt_value 10000
-ron_value 34
-tflight_value
-num_bobs 1
-capacity 80
-num_channels_per_bob 1
-first metric "Cost"
-second metric "Bandwidth"
-third metric "Energy"
-DIMM model "ALL"
-mirror_in_bob "F"
"""


_FIELDS = {
    "node_nm": re.compile(r"Technology size \(nm\):\s*([\d.]+)"),
    "access_ns": re.compile(r"Access time \(ns\):\s*([\d.eE+\-]+)"),
    "read_energy_nj": re.compile(r"Total dynamic read energy per access \(nJ\):\s*([\d.eE+\-]+)"),
    "write_energy_nj": re.compile(r"Total dynamic write energy per access \(nJ\):\s*([\d.eE+\-]+)"),
    "leakage_mw": re.compile(r"Total leakage power of a bank \(mW\):\s*([\d.eE+\-]+)"),
    "height_mm": re.compile(r"Cache height x width \(mm\):\s*([\d.eE+\-]+)\s*x\s*[\d.eE+\-]+"),
    "width_mm": re.compile(r"Cache height x width \(mm\):\s*[\d.eE+\-]+\s*x\s*([\d.eE+\-]+)"),
}


def parse_cacti(out: str) -> dict:
    vals = {}
    for key, rx in _FIELDS.items():
        m = rx.search(out)
        if not m:
            head = " | ".join(line for line in out.strip().splitlines()[:3])
            raise CactiError(f"CACTI output has no {key!r}: {head}")
        vals[key] = float(m.group(1))
    return vals


def run_cacti(size_bytes: int, block_bytes: int, node_nm: int, cacti: Path = CACTI_BIN) -> str:
    cacti = Path(cacti)
    if not cacti.is_file():
        raise CactiError(f"no CACTI binary at {cacti}")
    with tempfile.TemporaryDirectory(prefix="cacti_") as d:
        cfg = Path(d) / "sram.cfg"
        cfg.write_text(cacti_cfg(size_bytes, block_bytes, node_nm))
        r = subprocess.run([str(cacti), "-infile", str(cfg)], cwd=cacti.parent,
                           capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise CactiError(f"CACTI rc={r.returncode} for {size_bytes} B x {block_bytes} B: "
                         f"{r.stderr.strip()[:200]}")
    return r.stdout


def price_ram(depth: int, width_bits: int, node_nm: int = DEFAULT_NODE_NM,
              cacti: Path = CACTI_BIN) -> Priced:
    if depth < 1 or width_bits < 1:
        raise ValueError(f"bad SRAM shape: {depth} x {width_bits}")
    block = max(1, math.ceil(width_bits / 8))
    size = depth * block
    v = parse_cacti(run_cacti(size, block, node_nm, cacti))
    if v["node_nm"] != node_nm:
        raise CactiError(f"asked for {node_nm} nm, CACTI priced {v['node_nm']:g} nm")
    return Priced(
        depth=depth,
        width_bits=width_bits,
        block_bytes=block,
        size_bytes=size,
        node_nm=node_nm,
        area_um2=v["height_mm"] * v["width_mm"] * 1e6,
        read_energy_nj=v["read_energy_nj"],
        write_energy_nj=v["write_energy_nj"],
        leakage_mw=v["leakage_mw"],
        access_ns=v["access_ns"],
    )


def levels_of(config: dict) -> list[Level]:
    block = config.get("block_size", 64)
    missing = [n for n in LEVELS if n not in config]
    if missing:
        raise ValueError(f"config does not define {missing}; every level must be priced")
    return [Level(n, config[n]["sets"], config[n]["ways"], block) for n in LEVELS]


def _geometry_key(levels: list[Level], node_nm: int) -> str:
    parts = [f"{lv.name}{lv.sets}x{lv.ways}" for lv in levels]
    return "_".join(parts) + f"_b{levels[0].block_bytes}_{node_nm}nm"


def price_config(config_path: str | Path, node_nm: int = DEFAULT_NODE_NM,
                 prices_dir: Path = PRICES_DIR, cacti: Path = CACTI_BIN) -> dict:
    config = json.loads(Path(config_path).read_text())
    levels = levels_of(config)
    memo = Path(prices_dir) / f"{_geometry_key(levels, node_nm)}.json"
    if memo.is_file():
        return json.loads(memo.read_text())
    priced = {}
    for lv in levels:
        priced[lv.name] = {
            "sets": lv.sets,
            "ways": lv.ways,
            "block_bytes": lv.block_bytes,
            "tag_bits_per_way": tag_bits(lv.block_bytes),
            "data": asdict(price_ram(lv.data_depth, lv.data_width_bits, node_nm, cacti)),
            "tag": asdict(price_ram(lv.tag_depth, lv.tag_width_bits, node_nm, cacti)),
        }
    prices = {"geometry": memo.stem, "node_nm": node_nm, "levels": priced}
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(json.dumps(prices, indent=1) + "\n")
    return prices


def price_policy(policy_path: str | Path, sets: int, ways: int, cores: int = 1,
                 node_nm: int = DEFAULT_NODE_NM, cacti: Path = CACTI_BIN, words=None) -> dict:
    g = load_description(str(policy_path), words)
    b = metadata_budget(g, sets=sets, ways=ways, cores=cores)
    arrays = []
    for a in b.arrays:
        p = price_ram(a.depth, a.width_bits, node_nm, cacti)
        arrays.append({"name": a.name, "origin": a.origin, "declared_width": a.declared_width,
                       "bits": a.total_bits, **asdict(p)})
    registers = [{"name": r.name, "origin": r.origin, "width_bits": r.width_bits,
                  "instances": r.instances, "bits": r.total_bits} for r in b.registers]
    return {
        "policy": g.name,
        "sets": sets,
        "ways": ways,
        "cores": cores,
        "node_nm": node_nm,
        "arrays": arrays,
        "registers": registers,
        "sram_bits": b.sram_bits,
        "register_bits": b.register_bits,
        "array_bits_per_line": b.array_bits_per_line(),
        "bits_per_line": b.bits_per_line(),
        "area_um2": sum(a["area_um2"] for a in arrays),
    }


_PHASE = "=== Simulation ==="
_CPU_RE = re.compile(r"^CPU (\d+) cumulative IPC: \S+ instructions: (\d+) cycles: (\d+)", re.M)
_ACCESS_RE = re.compile(r"^cpu(\d+)->(?:cpu\d+_)?(\w+) TOTAL\s+ACCESS:\s+(\d+)", re.M)


ACCESS_TYPES = ("LOAD", "RFO", "PREFETCH", "WRITE", "TRANSLATION")
_CPU_PREFIX = re.compile(r"^cpu\d+_")


def sim_phase(data: list) -> dict:
    if not data:
        raise ValueError("empty JSON output")
    for entry in data:
        if entry.get("name") == "Simulation":
            return entry
    return data[-1]


def parse_json_run(data: list) -> RunStats:
    roi = sim_phase(data)["roi"]
    cores = roi["cores"]
    accesses: dict[str, int] = {}
    for name, blk in roi.items():
        if name in ("cores", "DRAM") or not isinstance(blk, dict):
            continue
        total = sum(sum(blk[t].get("hit", [])) + sum(blk[t].get("miss", []))
                    for t in ACCESS_TYPES if isinstance(blk.get(t), dict))
        short = _CPU_PREFIX.sub("", name)
        accesses[short] = accesses.get(short, 0) + total
    return RunStats(instructions=cores[0]["instructions"],
                    cycles=max(c["cycles"] for c in cores),
                    accesses=accesses)


def parse_log(log_path: str | Path) -> RunStats:
    text = Path(log_path).read_text()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        return parse_json_run(data)
    if _PHASE not in text:
        raise ValueError(f"{log_path}: no '{_PHASE}' block; the run did not finish")
    block = text.split(_PHASE, 1)[1]
    cpus = {int(c): (int(i), int(cy)) for c, i, cy in _CPU_RE.findall(block)}
    if 0 not in cpus:
        raise ValueError(f"{log_path}: no 'CPU 0 cumulative IPC' line after {_PHASE}")
    accesses: dict[str, int] = {}
    for _cpu, name, n in _ACCESS_RE.findall(block):
        accesses[name] = accesses.get(name, 0) + int(n)
    return RunStats(
        instructions=cpus[0][0],
        cycles=max(cy for _i, cy in cpus.values()),
        accesses=accesses,
    )


def energy(priced: dict, accesses: int, seconds: float) -> dict:
    dynamic = accesses * priced["read_energy_nj"]
    leakage = priced["leakage_mw"] * seconds * 1e6
    return {"dynamic_nj": dynamic, "leakage_nj": leakage, "energy_nj": dynamic + leakage}


def _sum(parts: list[dict]) -> dict:
    return {k: sum(p[k] for p in parts) for k in ("dynamic_nj", "leakage_nj", "energy_nj")}


def feedback(config_path: str | Path, policy_path: str | Path, log_path: str | Path,
             node_nm: int = DEFAULT_NODE_NM, prices_dir: Path = PRICES_DIR,
             cacti: Path = CACTI_BIN, words=None) -> dict:
    config = json.loads(Path(config_path).read_text())
    prices = price_config(config_path, node_nm, prices_dir, cacti)
    stats = parse_log(log_path)
    frequency_mhz = config["ooo_cpu"][0]["frequency"]
    seconds = stats.cycles / (frequency_mhz * 1e6)

    levels = {}
    for name, lv in prices["levels"].items():
        if name not in stats.accesses:
            raise ValueError(f"{log_path}: no TOTAL ACCESS line for {name}")
        acc = stats.accesses[name]
        parts = [energy(lv["data"], acc, seconds), energy(lv["tag"], acc, seconds)]
        levels[name] = {"accesses": acc,
                        "area_um2": lv["data"]["area_um2"] + lv["tag"]["area_um2"],
                        **_sum(parts)}

    llc = prices["levels"]["LLC"]
    policy = price_policy(policy_path, llc["sets"], llc["ways"], config["num_cores"],
                          node_nm, cacti, words)
    llc_accesses = stats.accesses["LLC"]
    meta_parts = [energy(a, llc_accesses, seconds) for a in policy["arrays"]]
    metadata = {**policy, "accesses": llc_accesses, **_sum(meta_parts)}
    if not meta_parts:
        metadata.update(dynamic_nj=0.0, leakage_nj=0.0, energy_nj=0.0)

    everything = list(levels.values()) + [metadata]
    total = {"area_um2": sum(e["area_um2"] for e in everything), **_sum(everything)}
    return {
        "node_nm": node_nm,
        "config": str(config_path),
        "policy": str(policy_path),
        "log": str(log_path),
        "frequency_mhz": frequency_mhz,
        "instructions": stats.instructions,
        "cycles": stats.cycles,
        "seconds": seconds,
        "levels": levels,
        "policy_metadata": metadata,
        "total": total,
    }


def report(fb: dict) -> str:
    lines = [f"CACTI {fb['node_nm']} nm   {fb['cycles']} cycles = {fb['seconds'] * 1e3:.3f} ms",
             f"  {'block':<10}{'accesses':>12}{'area mm2':>10}{'dynamic uJ':>12}{'leak uJ':>10}{'total uJ':>10}"]
    rows = list(fb["levels"].items()) + [("metadata", fb["policy_metadata"])]
    for name, e in rows:
        lines.append(f"  {name:<10}{e['accesses']:>12}{e['area_um2'] / 1e6:>10.4f}"
                     f"{e['dynamic_nj'] / 1e3:>12.2f}{e['leakage_nj'] / 1e3:>10.1f}"
                     f"{e['energy_nj'] / 1e3:>10.1f}")
    m = fb["policy_metadata"]
    lines.append(f"  metadata: {m['policy']}  {m['sram_bits'] / 8192:.2f} KB SRAM + "
                 f"{m['register_bits']} FF bits, {m['array_bits_per_line']:.2f} bits/line")
    t = fb["total"]
    lines.append(f"  total     {'':>12}{t['area_um2'] / 1e6:>10.4f}{t['dynamic_nj'] / 1e3:>12.2f}"
                 f"{t['leakage_nj'] / 1e3:>10.1f}{t['energy_nj'] / 1e3:>10.1f}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help="ChampSim config JSON")
    ap.add_argument("--policy", required=True, help="structure description YAML")
    ap.add_argument("--log", required=True, help="ChampSim output of the candidate's run")
    ap.add_argument("--node", type=int, default=DEFAULT_NODE_NM, help="process node in nm")
    ap.add_argument("--prices", type=Path, default=PRICES_DIR, help="where config prices are kept")
    ap.add_argument("--cacti", type=Path, default=CACTI_BIN, help="CACTI binary")
    ap.add_argument("--report", action="store_true", help="print a table instead of JSON")
    args = ap.parse_args()
    fb = feedback(args.config, args.policy, args.log, args.node, args.prices, args.cacti)
    print(report(fb) if args.report else json.dumps(fb, indent=1))
