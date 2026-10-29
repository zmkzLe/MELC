"""Assemble the LRU baseline the explore tier scores against (CHIA hackathon)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia_llc_loop import EXPLORE, VALIDATE, EXPLORE_WARMUP, EXPLORE_SIM

OUT = Path.home() / "loop_out"

SOURCES = [OUT / "probe_training_20M50M.json", OUT / "probe_retry_20M50M.json"]

MEASURED_BY_HAND = {
    "605.mcf_s-1644B": 0.19073328150118263,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="write lru_baseline.json; otherwise just report")
    ap.add_argument("--out", type=Path, default=OUT / "lru_baseline.json")
    args = ap.parse_args()

    lru: dict[str, float] = {}
    origin: dict[str, str] = {}
    for path in SOURCES:
        if not path.exists():
            print(f"missing source: {path}")
            continue
        blob = json.loads(path.read_text())
        if blob.get("warmup") != EXPLORE_WARMUP or blob.get("sim") != EXPLORE_SIM:
            print(f"REFUSING {path.name}: measured at "
                  f"{blob.get('warmup')}/{blob.get('sim')}, explore tier is "
                  f"{EXPLORE_WARMUP}/{EXPLORE_SIM}")
            return 1
        for stem, ipc in (blob.get("ipc", {}).get("lru") or {}).items():
            if ipc:
                lru[stem], origin[stem] = ipc, path.name
    for stem, ipc in MEASURED_BY_HAND.items():
        lru[stem], origin[stem] = ipc, "measured by hand"

    print(f"{len(lru)} traces at {EXPLORE_WARMUP / 1e6:.0f}M + "
          f"{EXPLORE_SIM / 1e6:.0f}M\n")

    absent = [t for t in EXPLORE if t not in lru]
    print(f"explore  {len(EXPLORE) - len(absent)}/{len(EXPLORE)} covered")
    for stem in EXPLORE:
        mark = f"{lru[stem]:.4f}  {origin[stem]}" if stem in lru else "MISSING"
        print(f"  {stem:26s} {mark}")

    held = [t for t in VALIDATE if t not in lru]
    print(f"\nheld-out {len(VALIDATE) - len(held)}/{len(VALIDATE)} covered")
    for stem in held:
        print(f"  {stem:26s} MISSING")

    if absent:
        print(f"\n{len(absent)} explore trace(s) have no baseline; scoring would "
              f"silently rank candidates on the rest. Not writing.")
        return 1

    if not args.write:
        print("\nlooks complete; re-run with --write to save")
        return 0

    args.out.write_text(json.dumps(lru, indent=2, sort_keys=True))
    print(f"\nwrote {args.out} ({len(lru)} traces)")
    if held:
        print(f"note: {len(held)} held-out trace(s) still missing -- fine for "
              f"searching, but the validation tier needs them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
