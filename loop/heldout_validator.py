from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chia_llc_loop as loop
from candidate import module_sources
from chia_node.champsim import build_replacement

SEARCH_TREE = (Path.home() / "champsim").resolve()
SEARCH_MODULE = "evolved_policy"
DONE_EVENTS = ("validated", "failed")


def parse_time(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"{text}: give a timezone (Z or +hh:mm)")
    return dt.timestamp()


def iso(t: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(t if t is not None else time.time(),
                                           datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def bests(records: list[dict]) -> list[dict]:
    seq, top = [], -math.inf
    for r in records:
        if r.get("status") == "ok" and isinstance(r.get("score"), (int, float)) and r["score"] > top:
            top = r["score"]
            seq.append(r)
    return seq


def load_baseline(path: str, traces: list[str]) -> dict:
    base = json.loads(Path(path).read_text())
    assert isinstance(base, dict) and base and all(isinstance(v, (int, float)) and v > 0 for v in base.values()), \
        f"{path}: not a dict of positive IPCs"
    missing = [s for s in traces if s not in base]
    assert not missing, f"{path}: no baseline for {missing}"
    selected = loop.EXPLORE + loop.VALIDATE
    if set(traces) == set(selected):
        assert set(base) == set(selected) and len(base) == len(selected), \
            f"{path}: its keys are not exactly the {len(selected)} selected traces"
    return base


def vs_reference(ipcs: dict, reference: dict, traces: list[str]) -> tuple[dict, dict]:
    ratios = {s: ipcs[s] / reference[s] for s in traces}

    def gm(group):
        sub = [ratios[s] for s in traces if group is None or s in group]
        return math.exp(sum(math.log(r) for r in sub) / len(sub)) if sub else None
    return {"all": gm(None), "training": gm(loop.EXPLORE), "held_out": gm(loop.VALIDATE)}, ratios


def seed_of(records: list[dict]) -> dict | None:
    return next((r for r in records if r.get("status") == "ok" and "id" in r), None)


def seed_ipcs(path: Path, sid: str) -> dict | None:
    line = next((e for e in read_jsonl(path) if e.get("event") == "validated" and e.get("id") == sid), None)
    return line["ipc"] if line else None


def longest_first(traces: list[str], durations: dict) -> list[str]:
    return sorted(traces, key=lambda s: -durations.get(s, math.inf))


def expected_seconds(events: list[dict], default: float) -> float:
    real = [e["seconds"] for e in events if e.get("event") == "validated" and not e.get("fake") and e.get("seconds")]
    return max(real) if real else default


class Log:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()

    def append(self, event: str, **fields) -> None:
        with self.lock, open(self.path, "a") as f:
            f.write(json.dumps({"event": event, "time": iso(), **fields}) + "\n")
        brief = {k: v for k, v in fields.items() if k in ("id", "score", "by", "stage", "error", "seconds", "gap")}
        print(f"[{iso()}] {event} {json.dumps(brief)}", flush=True)


def validate_one(rec: dict, a: argparse.Namespace, base: dict, durations: dict, log: Log, sid: str) -> None:
    did, t0 = rec["id"], time.time()
    work = a.run_dir / "validation" / did
    work.mkdir(parents=True, exist_ok=True)
    training = {"score": rec["score"], "lengths": rec.get("lengths"), "traces": len(rec.get("traces") or []),
                "time": rec.get("time")}
    built, run_seconds = None, {}
    try:
        if a.fake_seconds is not None:
            time.sleep(a.fake_seconds)
            ipcs = {s: base.get(s, 1.0) for s in a.traces}
        else:
            store = a.run_dir / "store" / did
            sources = module_sources((store / "policy.h").read_text(), (store / "policy.cc").read_text(), a.module)
            built = build_replacement(str(a.tree), a.module, sources, timeout_s=a.build_timeout)
            (work / "build.log").write_text(built.build_diagnostics or built.stdout_tail)
            if not built.success:
                log.append("failed", id=did, stage="build", training=training, seconds=round(time.time() - t0, 1),
                           error=built.build_diagnostics[-2000:])
                return
            assert built.binary and built.source_id, "a build reported success without a binary or a source id"
            runs = loop.run_traces(built.binary, longest_first(a.traces, durations), opt_log_dir=None,
                                   tag="validate", warmup=a.warmup, sim=a.sim, timeout_s=a.sim_timeout)
            assert set(runs) == set(a.traces), f"runs for {sorted(runs)}, asked for {sorted(a.traces)}"
            run_seconds = {s: round(r.wall_s, 1) for s, r in runs.items()}
            ipcs = {s: (r.ipc if r.success else None) for s, r in runs.items()}
            bad = sorted(s for s, v in ipcs.items() if not v)
            if bad:
                (work / "run_failures.json").write_text(json.dumps(
                    {s: {"timed_out": runs[s].timed_out, "tail": runs[s].stdout_tail[-800:]} for s in bad}, indent=1))
                log.append("failed", id=did, stage="run", training=training, seconds=round(time.time() - t0, 1),
                           run_seconds=run_seconds, timed_out=[s for s in bad if runs[s].timed_out],
                           error=f"{len(bad)} of {len(a.traces)} traces did not finish: {', '.join(bad)}")
                return
        assert all(ipcs.get(s) for s in a.traces), "a trace has no IPC"
        (work / "ipc.json").write_text(json.dumps(ipcs, indent=1))
        reference = ipcs if did == sid else seed_ipcs(log.path, sid)
        if reference is None or not all(reference.get(s) for s in a.traces):
            log.append("failed", id=did, stage="reference", training=training, seconds=round(time.time() - t0, 1),
                       ipc=ipcs, error=f"no validated IPC of the seed {sid} on every trace to compare with")
            return
        parts, ratios = vs_reference(ipcs, reference, a.traces)
        gap = ({"difference": parts["training"] - parts["held_out"], "ratio": parts["training"] / parts["held_out"]}
               if parts["training"] and parts["held_out"] else None)
        log.append("validated", id=did, training=training, lengths=[a.warmup, a.sim], traces=len(a.traces),
                   reference=sid, score=parts, gap=gap, vs_mj=ratios, ipc=ipcs,
                   seconds=round(time.time() - t0, 1),
                   build_seconds=round(built.build_duration_s, 1) if built else 0,
                   cleaned=built.cleaned if built else None, source_id=built.source_id if built else None,
                   run_seconds=run_seconds, module=a.module, tree=str(a.tree), fake=a.fake_seconds is not None)
    except Exception as exc:
        log.append("failed", id=did, stage="error", training=training, seconds=round(time.time() - t0, 1),
                   error=f"{type(exc).__name__}: {exc}"[:2000])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="the evaluator's run directory (store/, records.jsonl)")
    ap.add_argument("--tree", default=str(Path.home() / "champsim_validate"), help="the validator's own ChampSim tree")
    ap.add_argument("--module", default="validate_policy")
    ap.add_argument("--baseline", default=None, help="testing with --fake-seconds only: the fake IPC per trace")
    ap.add_argument("--durations", default=None, help="seconds per trace at this length; the longest start first")
    ap.add_argument("--traces", default=None, help="comma-separated override (rehearsals); default all 33")
    ap.add_argument("--warmup", type=int, default=loop.VALIDATE_WARMUP)
    ap.add_argument("--sim", type=int, default=loop.VALIDATE_SIM)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--sim-timeout", type=int, default=18_000, help="per trace; gcc_s alone is 159 min (default 5 h)")
    ap.add_argument("--build-timeout", type=int, default=1800)
    ap.add_argument("--poll", type=float, default=60)
    ap.add_argument("--search-stop", default=None, help="ISO time; from then on two validations run at once")
    ap.add_argument("--deadline", default=None, help="ISO time; a validation that would end after it is deferred")
    ap.add_argument("--expect-seconds", type=float, default=11_400,
                    help="estimated validation before one has finished (default 3 h 10 min)")
    ap.add_argument("--min-gain", type=float, default=0.001,
                    help="a best is validated only if its training score beats the last validated design's by this "
                         "fraction; smaller bests are logged below_min_gain and skipped (0: every best)")
    ap.add_argument("--drain", action="store_true", help="validate the deferred designs (and the best), then exit")
    ap.add_argument("--once", action="store_true", help="exit when nothing runs and nothing can start")
    ap.add_argument("--fake-seconds", type=float, default=None,
                    help="testing: no build, no runs, IPC = --baseline's (1.0 without one)")
    a = ap.parse_args()

    a.run_dir, a.tree = Path(a.run_dir).expanduser().resolve(), Path(a.tree).expanduser().resolve()
    a.traces = a.traces.split(",") if a.traces else loop.EXPLORE + loop.VALIDATE
    assert a.run_dir.is_dir(), f"{a.run_dir}: no such run directory"
    if a.fake_seconds is None:
        assert a.tree.is_dir() and (a.tree / "replacement").is_dir() and (a.tree / "config.sh").exists(), \
            f"{a.tree}: not a ChampSim tree"
        assert a.tree != SEARCH_TREE, f"the validator must not build in the search's tree {SEARCH_TREE}"
        assert a.module != SEARCH_MODULE, f"the validator must not build under the search's module {SEARCH_MODULE}"
        gone = loop.absent(a.traces)
        assert not gone, f"{len(gone)} trace(s) not on this machine: {', '.join(gone)}"
    durations = json.loads(Path(a.durations).read_text()) if a.durations else {}
    base = load_baseline(a.baseline, a.traces) if a.baseline else {}
    loop.WORKERS = a.workers
    stop, deadline = parse_time(a.search_stop), parse_time(a.deadline)
    log = Log(a.run_dir / "validation.jsonl")
    running: dict[str, threading.Thread] = {}
    print(f"validator: {a.run_dir}\n  tree {a.tree} module {a.module}\n  {len(a.traces)} traces at {a.warmup} + {a.sim}, "
          f"{a.workers} workers, sim timeout {a.sim_timeout} s\n  search stop {iso(stop) if stop else '-'}, "
          f"deadline {iso(deadline) if deadline else '-'}, drain {a.drain}, fake {a.fake_seconds}", flush=True)
    if not (a.run_dir / "store").is_dir():
        print(f"waiting for {a.run_dir / 'store'}", flush=True)
        while not (a.run_dir / "store").is_dir():
            time.sleep(a.poll)

    while True:
        for did, th in list(running.items()):
            if not th.is_alive():
                del running[did]
        records = read_jsonl(a.run_dir / "records.jsonl")
        events = read_jsonl(log.path)
        seq = bests(records)
        done = {e["id"] for e in events if e.get("event") in DONE_EVENTS}
        deferred = [e["id"] for e in events if e.get("event") == "deferred" and e["id"] not in done]
        superseded = {e["id"] for e in events if e.get("event") == "superseded"}
        small = {e["id"] for e in events if e.get("event") == "below_min_gain"}
        started = [e["score"] for e in events if e.get("event") == "started" and isinstance(e.get("score"), (int, float))]
        reference = max(started) if started else None
        now = time.time()
        current = seq[-1] if seq else None
        seed = seed_of(records)
        sid = seed["id"] if seed else None
        seed_done = sid in done
        seed_failed = any(e.get("event") == "failed" and e.get("id") == sid for e in events)

        if current and reference is not None and current["id"] != sid and current["id"] not in done | superseded | small \
                and current["id"] not in running and current["score"] < reference * (1 + a.min_gain):
            log.append("below_min_gain", id=current["id"], score=current["score"], reference=reference,
                       min_gain=a.min_gain)
            small.add(current["id"])

        if current:
            for r in seq[:-1]:
                if r["id"] != sid and r["id"] not in done and r["id"] not in deferred and r["id"] not in superseded \
                        and r["id"] not in small and r["id"] not in running:
                    log.append("superseded", id=r["id"], score=r["score"], by=current["id"], by_score=current["score"])
                    superseded.add(r["id"])

        targets = []
        if current and current["id"] not in done and current["id"] not in superseded and current["id"] not in small \
                and current["id"] not in running:
            if a.drain or current["id"] not in deferred:
                targets.append(current)
        if a.drain:
            by_id = {r["id"]: r for r in records if r.get("status") == "ok" and "id" in r}
            for did in deferred:
                if did in by_id and did not in running and all(t["id"] != did for t in targets):
                    targets.append(by_id[did])
        slots = 2 if a.drain or (stop is not None and now >= stop) else 1
        if seed and seed_failed:
            targets = []
        elif seed and not seed_done:
            targets = [seed] if sid not in running and (a.drain or sid not in deferred) else []
            slots = 1

        for r in targets:
            if len(running) >= slots:
                break
            if deadline is not None and not a.drain:
                expect = expected_seconds(events, a.expect_seconds)
                if now + expect > deadline:
                    log.append("deferred", id=r["id"], score=r["score"], expected_seconds=expect, deadline=iso(deadline))
                    continue
            log.append("started", id=r["id"], score=r["score"], slots=slots)
            th = threading.Thread(target=validate_one, args=(r, a, base, durations, log, sid), name=r["id"])
            th.start()
            running[r["id"]] = th

        idle = not running and not any(t["id"] not in running for t in targets if t["id"] not in done)
        if (a.once or a.drain) and idle:
            break
        if deadline is not None and now >= deadline and not running:
            print(f"[{iso()}] past the deadline with nothing running: done", flush=True)
            break
        time.sleep(a.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
