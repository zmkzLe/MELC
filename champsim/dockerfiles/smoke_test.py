"""What a worker must be able to do, checked while the image is built.

    python3 dockerfiles/smoke_test.py [--champsim ROOT] [--trace TRACE]

Builds a replacement policy through the CHIA node, runs it with the OPT log on,
and turns that run into a profile with CACTI. Whatever is missing from an image
-- the profiling counters, the OPT switch, CACTI, the feedback code, a Python
dependency -- fails here rather than in the middle of a loop. The candidate is
ChampSim's own LRU under another name, so the run has a known answer.

Exits 0 if everything passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from chia_node.champsim import build_replacement, run_simulation  # noqa: E402

MODULE = "smoke_probe"
WARMUP, SIM = 1000, 5000
CAUSES = ("retiring", "frontend", "speculation", "backend_core", "backend_memory")


def renamed_lru(champsim: Path) -> dict:
    """ChampSim's LRU as a candidate: a .cc and a .h, which CHIA's node cannot place."""
    out = {}
    for name in ("lru.h", "lru.cc"):
        text = (champsim / "replacement" / "lru" / name).read_text()
        text = text.replace("REPLACEMENT_LRU_H", f"REPLACEMENT_{MODULE.upper()}_H")
        text = text.replace('"lru.h"', f'"{MODULE}.h"').replace("lru", MODULE)
        out[name.replace("lru", MODULE)] = text
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--champsim", type=Path, default=ROOT)
    ap.add_argument("--trace", type=Path, default=None)
    ap.add_argument("--policy", type=Path, default=ROOT / "structure_description" / "policies" / "lru.yaml")
    args = ap.parse_args()
    champsim = args.champsim.resolve()
    trace = args.trace or champsim / "test" / "traces" / "smoke.champsimtrace.gz"
    if not Path(trace).is_file():
        print(f"FAIL  no trace at {trace}")
        return 1

    build = build_replacement(str(champsim), MODULE, renamed_lru(champsim))
    if not build.success:
        print(f"FAIL  build rc={build.returncode}\n{build.build_diagnostics[-2000:]}")
        return 1
    llc = build.config["LLC"]
    print(f"ok    built {build.executable_name} in {build.build_duration_s:.0f}s, "
          f"LLC {llc['sets']}x{llc['ways']}, replacement {llc['replacement']!r}")
    if (llc["sets"], llc["ways"]) != (4096, 12):
        print("FAIL  the DPC4 machine is not what was built")
        return 1

    with tempfile.TemporaryDirectory() as d:
        stats_path = Path(d) / "run.json"
        opt_log = Path(d) / "run.opt.log"
        run = run_simulation(build.binary, str(trace), warmup_instructions=WARMUP,
                             simulation_instructions=SIM, json_path=str(stats_path),
                             opt_log=str(opt_log), opt_sets=256)
        if not run.success:
            print(f"FAIL  run rc={run.returncode}\n{run.stdout_tail[-2000:]}")
            return 1
        print(f"ok    ran {run.instructions} instructions, IPC {run.ipc:.3f}")

        roi = next(e for e in run.stats if e.get("name") == "Simulation")["roi"]
        cause = roi["cores"][0].get("cycles by cause", {})
        missing = [c for c in CAUSES if c not in cause]
        if missing or "backend memory cycles" not in roi["cores"][0]:
            print(f"FAIL  this ChampSim does not report the profile: missing {missing}")
            return 1
        if cause["residual"] != 0:
            print(f"FAIL  the cycle accounting does not add up: residual {cause['residual']}")
            return 1
        print(f"ok    profiling counters present, residual 0, "
              f"{'mshr occupancy' in roi['LLC'] and 'MSHR occupancy'} reported")

        header = opt_log.read_text(errors="replace").splitlines()[0]
        if not header.startswith("H 4096 12 64 256"):
            print(f"FAIL  the OPT log header is {header!r}")
            return 1
        print(f"ok    OPT log {run.opt_log_bytes} bytes, header {header!r}")

        from feedback.area_energy import price_ram  # noqa: PLC0415
        priced = price_ram(4096, 24, node_nm=22)
        if not priced.area_um2 > 0:
            print("FAIL  CACTI priced nothing")
            return 1
        print(f"ok    CACTI at 22 nm: 4096 x 24 bits is {priced.area_um2:.0f} um2")

        from feedback.profile import profile, report  # noqa: PLC0415
        config_path = Path(d) / "config.json"
        config_path.write_text(json.dumps(build.config))
        p = profile(config_path, args.policy, stats_path)
        if abs(p["checks"]["cycle_residual"]) > 1e-12 or abs(p["checks"]["memory_split_residual"]) > 1e-12:
            print(f"FAIL  the profile does not balance: {p['checks']}")
            return 1
        print("ok    profile from the run's JSON:")
        print("      " + report(p).replace("\n", "\n      "))

    shutil.rmtree(champsim / "replacement" / MODULE, ignore_errors=True)
    (champsim / "bin" / build.executable_name).unlink(missing_ok=True)
    print("\nsmoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
