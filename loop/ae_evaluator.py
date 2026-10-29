from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia_llc_loop import (
    EXPLORE,
    WARMUP,
    SIM,
    CactiError,
    absent,
    build,
    cacti_metrics,
    structure_description_is_legal,
    run_traces,
)

from candidate import (
    HEADER,
    MODULE,
    SOURCE,
    STRUCTURE_DESCRIPTION,
    module_sources,
)

_SECTION = re.compile(
    rf"^=====\s*({STRUCTURE_DESCRIPTION}|{HEADER}|{SOURCE})\s*=====\s*$", re.M)


def split_candidate(text: str) -> dict[str, str]:
    parts, marks = {}, list(_SECTION.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts[m.group(1)] = text[m.end():end].strip("\n")
    return parts


RUN = Path(os.environ.get("A3_RUN_DIR") or Path.home() / "ae_run")
STORE, RECORDS = RUN / "store", RUN / "records.jsonl"


def design_id(header: str, source: str) -> str:
    return hashlib.sha256(f"{header}\n{source}".encode()).hexdigest()[:12]


def reference_ipcs() -> dict | None:
    if not RECORDS.exists():
        return None
    for line in RECORDS.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("status") == "ok" and rec.get("ipc") \
                and rec.get("lengths") == [WARMUP, SIM] and rec.get("traces") == EXPLORE:
            return rec["ipc"]
    return None


def vs_mockingjay(ipcs: dict, reference: dict) -> tuple[float, dict]:
    ratios = {s: ipcs[s] / reference[s] for s in EXPLORE}
    return math.exp(sum(math.log(r) for r in ratios.values()) / len(ratios)), ratios


def remember(did: str, sources: dict[str, str], metrics: dict) -> None:
    if sources:
        design = STORE / did
        design.mkdir(parents=True, exist_ok=True)
        for name, text in sources.items():
            path = design / name
            if not path.exists():
                path.write_text(text)
    RECORDS.parent.mkdir(parents=True, exist_ok=True)
    with RECORDS.open("a") as fh:
        fh.write(json.dumps({"id": did, "at": round(time.time(), 1),
                             "lengths": [WARMUP, SIM], "traces": EXPLORE,
                             **metrics}) + "\n")


def refused(metrics: dict, parts: dict | None = None) -> dict:
    text = "".join((parts or {}).get(k, "") for k in (STRUCTURE_DESCRIPTION, HEADER, SOURCE))
    remember(hashlib.sha256(text.encode()).hexdigest()[:12], {}, metrics)
    return metrics


def evaluate(program_path: str) -> dict:
    text = Path(program_path).read_text()
    parts = split_candidate(text)

    sections = (STRUCTURE_DESCRIPTION, HEADER, SOURCE)
    missing = [s for s in sections if not parts.get(s)]
    if missing:
        return refused({"combined_score": 0.0, "status": "malformed",
                        "error": f"candidate is missing section(s): {', '.join(missing)}"}, parts)

    legal, why = structure_description_is_legal(parts[STRUCTURE_DESCRIPTION])
    if not legal:
        return refused({"combined_score": 0.0, "status": "schema_rejected", "error": why}, parts)

    missing = absent(EXPLORE)
    if missing:
        return refused({"combined_score": 0.0, "status": "missing_traces",
                        "error": f"{len(missing)} training trace(s) not on this machine: "
                                 f"{', '.join(missing)}"}, parts)

    built = build(MODULE, module_sources(parts[HEADER], parts[SOURCE]))
    if not built.success:
        return refused({"combined_score": 0.0, "status": "build_error",
                        "error": built.build_diagnostics[-3000:]}, parts)

    runs = run_traces(built.binary, EXPLORE, tag=built.source_id)
    ipcs = {stem: (run.ipc if run.success else None) for stem, run in runs.items()}

    failed = sorted(s for s, v in ipcs.items() if not v)
    if failed:
        return refused({"combined_score": 0.0, "status": "incomplete",
                        "error": f"{len(failed)} of {len(EXPLORE)} traces did not finish: "
                                 f"{', '.join(failed)}",
                        "ipc": ipcs}, parts)

    reference = reference_ipcs() or ipcs
    combined, relative = vs_mockingjay(ipcs, reference)
    try:
        cost = cacti_metrics(parts[STRUCTURE_DESCRIPTION], built.config, runs)
    except CactiError as exc:
        cost = {"cacti_error": str(exc)[:500]}
    metrics = {"combined_score": combined, "status": "ok", **cost,
               "ipc": ipcs, "relative": relative,
               "llc": f"{built.config['LLC']['sets']}x{built.config['LLC']['ways']}"}
    remember(design_id(parts[HEADER], parts[SOURCE]),
             {"policy.h": parts[HEADER], "policy.cc": parts[SOURCE],
              "description.yaml": parts[STRUCTURE_DESCRIPTION]},
             {"score": combined, **metrics})
    return metrics


if __name__ == "__main__":
    import json
    print(json.dumps(evaluate(sys.argv[1]), indent=2, default=str))
