from __future__ import annotations

import datetime
import difflib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cpp_writer
from candidate import MARKER, MODULE, POLICIES, STRUCTURE_DESCRIPTION, module_sources
from chia_llc_loop import (CHAMPSIM, EXPLORE, SIM, WARMUP, CactiError, absent, build,
                           cacti_metrics, run_traces)
from feedback import opt as opt_tool
from feedback.profile import profile as profile_run
from structure_description import new_words, validate
from structure_description.cacti import budget as metadata_budget
from chia_node.champsim import DEFAULT_BASE_CONFIG

NEW_WORDS = "NEW_WORDS"
RUN = Path(os.environ.get("A3_RUN_DIR", Path.home() / "oe_out" / "yaml_run"))
TRACES = os.environ["A3_TRACES"].split(",") if os.environ.get("A3_TRACES") else EXPLORE
RUN_WARMUP = int(os.environ.get("A3_WARMUP", WARMUP))
RUN_SIM = int(os.environ.get("A3_SIM", SIM))
CPP_MODEL = os.environ.get("A3_CPP_MODEL", "gemini-2.5-pro")
REPAIRS = int(os.environ.get("A3_REPAIRS", 2))
AUDIT = os.environ.get("A3_AUDIT", "1") != "0"
LLM = {"max_tokens": int(os.environ.get("A3_CPP_MAX_TOKENS", "32000")),
       "timeout": int(os.environ.get("A3_CPP_TIMEOUT", "300")), "retries": 1}
MAX_KB = float(os.environ.get("A3_MAX_KB") or 48)

STORE, REGISTRY, RECORDS = RUN / "store", RUN / "new_words_registry.jsonl", RUN / "records.jsonl"
SEED_YAML = POLICIES / "mockingjay.yaml"
SEED_CPP = HERE / "seed_cpp"
OPT_PY = CHAMPSIM / "feedback" / "opt.py"
WHY_MAX = 20_000
MISTAKES = ("kept_dead", "evicted_sooner", "should_bypass", "wrong_bypass")

_SECTION = re.compile(rf"^=====\s*({STRUCTURE_DESCRIPTION}|{NEW_WORDS})\s*=====\s*$", re.M)


def split_candidate(text: str) -> dict[str, str]:
    parts, marks = {}, list(_SECTION.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].splitlines()
        parts[m.group(1)] = "\n".join(line for line in body if not line.startswith("```")).strip("\n")
    return parts


from evolver_prompt import canonical, limits, packed, without_doc

MAX_CHANGE, MAX_LINES = limits()


def declared_kb(description: str, words) -> float:
    cfg = json.loads((CHAMPSIM / DEFAULT_BASE_CONFIG).read_text())
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(description)
        path = fh.name
    try:
        b = metadata_budget(validate.load(path, words), sets=cfg["LLC"]["sets"], ways=cfg["LLC"]["ways"],
                            cores=cfg["num_cores"])
    finally:
        Path(path).unlink(missing_ok=True)
    return (b.sram_bits + b.register_bits) / 8192


def step_size(base_canon: str, canon: str) -> int:
    return sum(1 for line in difflib.unified_diff(base_canon.splitlines(), canon.splitlines(), n=0, lineterm="")
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def design_id(description: str, words: str) -> str:
    return hashlib.sha256(canonical(description, words).encode()).hexdigest()[:12]


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def add_to_store(did: str, description: str, words: str, header: str, source: str,
                 result: dict | None = None, opt_dir: Path | None = None) -> None:
    final = STORE / did
    if not final.exists():
        tmp = STORE / f".{did}.{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for name, text in (("description.yaml", description), ("words.yaml", words),
                           ("policy.h", header), ("policy.cc", source)):
            (tmp / name).write_text(text)
        try:
            os.rename(tmp, final)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
    if result is not None and not (final / "result.json").exists():
        if opt_dir is not None and opt_dir.is_dir():
            shutil.copytree(opt_dir, final / "opt", dirs_exist_ok=True)
        _write_atomic(final / "result.json", json.dumps(result, indent=1))


def load_designs() -> list[dict]:
    out = []
    for d in sorted(STORE.glob("[!.]*")):
        if not (d / "policy.cc").exists():
            continue
        e = {"id": d.name, "dir": d, **{k: (d / f).read_text() for k, f in (
            ("description", "description.yaml"), ("words", "words.yaml"),
            ("header", "policy.h"), ("source", "policy.cc"))}}
        e["canonical"] = canonical(e["description"], e["words"])
        e["result"] = json.loads((d / "result.json").read_text()) if (d / "result.json").exists() else None
        out.append(e)
    return out


def seed_id() -> str:
    description = SEED_YAML.read_text()
    did = design_id(description, "")
    add_to_store(did, description, "", (SEED_CPP / "policy.h").read_text(), (SEED_CPP / "policy.cc").read_text())
    return did


def scored_before(did: str, designs: list[dict]) -> dict | None:
    same = next((d for d in designs if d["id"] == did and d["result"]), None)
    if same is None or same["result"].get("lengths") != [RUN_WARMUP, RUN_SIM] or same["result"].get("traces") != TRACES:
        return None
    if not RECORDS.exists():
        return None
    for line in reversed(RECORDS.read_text().splitlines()):
        if line.strip():
            r = json.loads(line)
            if r.get("id") == did and r.get("status") == "ok" and "signals" in r:
                assert r["score"] == same["result"]["score"], f"{did}: the record and the store disagree on the score"
                return r
    return None


def closest(canon: str, did: str, designs: list[dict]) -> tuple[dict | None, float]:
    lines = canon.splitlines()
    best, ratio = None, -1.0
    for d in designs:
        if d["id"] == did:
            continue
        r = difflib.SequenceMatcher(None, d["canonical"].splitlines(), lines, autojunk=False).ratio()
        if r > ratio:
            best, ratio = d, r
    return best, ratio


def touched(a, b, path: str = "", depth: int = 2) -> list[str]:
    if a == b:
        return []
    if depth == 0 or not (isinstance(a, dict) and isinstance(b, dict)):
        return [path or "(everything)"]
    out = []
    for k in sorted(set(a) | set(b), key=str):
        out += touched(a.get(k), b.get(k), f"{path}.{k}" if path else str(k), depth - 1)
    return out


def _log_call(rec: dict, work: Path, name: str, got: dict, base: str, built=None, error: str = "") -> None:
    cpp_writer.save({k: got[k] for k in ("model", "usage", "seconds", "reply") if k in got}, work / "cpp", f"{name}_")
    if built is not None:
        (work / "cpp" / f"{name}_build.txt").write_text(built.build_diagnostics or "ok")
    rec["cpp_calls"].append({"call": name, "base": base, "usage": got.get("usage", {}),
                             "seconds": got.get("seconds"), "built": None if built is None else built.success,
                             "build_s": None if built is None else round(built.build_duration_s, 1),
                             "cleaned": None if built is None else built.cleaned,
                             **({"error": error[:500]} if error else {})})


def write_cpp(description: str, words: str, did: str, near: dict | None, seed: dict,
              work: Path, rec: dict):
    same = next((d for d in load_designs() if d["id"] == did), None)
    if same is not None:
        built = build(MODULE, module_sources(same["header"], same["source"]))
        if built.success:
            rec["cpp"] = {"reused": did}
            return same["header"], same["source"], built, ""
    last = "no attempt made"
    for attempt, base in enumerate((near or seed, seed), 1):
        try:
            got = cpp_writer.edit(description, words, base["description"], base["words"], base["header"],
                                 base["source"], model=CPP_MODEL, **LLM)
        except cpp_writer.Unusable as exc:
            _log_call(rec, work, f"a{attempt}_edit", exc.got, base["id"], error=str(exc))
            last = f"the C++ agent's reply held no usable C++: {exc}"
            continue
        except RuntimeError as exc:
            rec["cpp_calls"].append({"call": f"a{attempt}_edit", "base": base["id"], "error": str(exc)[:500]})
            last = str(exc)
            continue
        msgs = got["messages"]
        built = build(MODULE, module_sources(got["header"], got["source"]))
        _log_call(rec, work, f"a{attempt}_edit", got, base["id"], built)
        for r in range(1, REPAIRS + 1):
            if built.success:
                break
            try:
                got = cpp_writer.repair("", got["reply"], built.build_diagnostics.replace(MODULE, "@MODULE@"),
                                       model=CPP_MODEL, msgs=msgs, **LLM)
            except cpp_writer.Unusable as exc:
                _log_call(rec, work, f"a{attempt}_repair{r}", exc.got, base["id"], error=str(exc))
                break
            except RuntimeError as exc:
                rec["cpp_calls"].append({"call": f"a{attempt}_repair{r}", "base": base["id"], "error": str(exc)[:500]})
                break
            built = build(MODULE, module_sources(got["header"], got["source"]))
            _log_call(rec, work, f"a{attempt}_repair{r}", got, base["id"], built)
        if built.success:
            rec["cpp"] = {"base": base["id"], "attempt": attempt}
            return (*audit_edit(description, words, base, got, built, work, rec, f"a{attempt}"), "")
        last = f"the C++ did not build:\n{built.build_diagnostics[-2500:]}"
    restored = build(MODULE, module_sources(seed["header"], seed["source"]))
    rec["cpp"] = {"failed": True, "seed_restored": restored.success}
    if not restored.success:
        raise RuntimeError("the seed C++ did not build back into the tree after a failed candidate:\n"
                           + restored.build_diagnostics[-2000:])
    return None, None, None, last


def audit_edit(description: str, words: str, base: dict, got: dict, built, work: Path, rec: dict, name: str):
    header, source = got["header"], got["source"]
    before = cpp_writer.changed_lines(base["header"], base["source"], header, source)
    rec["cpp"]["edit_lines"] = before
    if not AUDIT or before == 0:
        return header, source, built
    try:
        aud = cpp_writer.audit(description, words, base["description"], base["words"], base["header"],
                              base["source"], header, source, model=CPP_MODEL, **LLM)
    except cpp_writer.Unusable as exc:
        _log_call(rec, work, f"{name}_audit", exc.got, base["id"], error=str(exc))
        rec["cpp"]["audit"] = {"kept": False, "why": f"unusable reply: {exc}"[:300]}
        return header, source, built
    except RuntimeError as exc:
        rec["cpp_calls"].append({"call": f"{name}_audit", "base": base["id"], "error": str(exc)[:500]})
        rec["cpp"]["audit"] = {"kept": False, "why": str(exc)[:300]}
        return header, source, built
    after = cpp_writer.changed_lines(base["header"], base["source"], aud["header"], aud["source"])
    if not 0 < after < before:
        _log_call(rec, work, f"{name}_audit", aud, base["id"])
        rec["cpp"]["audit"] = {"lines": after, "kept": False, "why": "reverted everything" if after == 0
                               else "nothing reverted" if after == before else "larger than the edit"}
        return header, source, built
    audited = build(MODULE, module_sources(aud["header"], aud["source"]))
    _log_call(rec, work, f"{name}_audit", aud, base["id"], audited)
    if audited.success:
        rec["cpp"]["audit"] = {"lines": after, "kept": True}
        return aud["header"], aud["source"], audited
    built = build(MODULE, module_sources(header, source))
    rec["cpp_calls"].append({"call": f"{name}_edit_back", "base": base["id"], "built": built.success,
                             "build_s": round(built.build_duration_s, 1), "cleaned": built.cleaned})
    assert built.success, "the edit that built before the audit does not build back"
    rec["cpp"]["audit"] = {"lines": after, "kept": False, "why": "did not build"}
    return header, source, built


def trace_signals(stem: str, run, work: Path, words, near_opt: Path | None) -> tuple[str, dict]:
    out: dict = {}
    stats = work / "stats" / f"{stem}.json"
    stats.write_text(json.dumps(run.stats))
    try:
        p = profile_run(work / "config.json", work / "description.yaml", stats, words=words)
        out["profile"] = {"llc_mpki": p["blocks"]["LLC"]["mpki"], "cycles_by_cause": p["cycles_by_cause"],
                          "memory_by_level": p["backend_memory_by_level"],
                          "dram_share": p["backend_memory_by_level"].get("dram")}
    except Exception as exc:
        out["profile"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    log, result = Path(run.opt_log_path), work / "opt" / f"{stem}.json"
    done = subprocess.run([sys.executable, str(OPT_PY), str(log), "--out", str(result)],
                          capture_output=True, text=True, timeout=3600)
    if done.returncode == 0:
        r = json.loads(result.read_text())
        out["opt"] = {k: r[k] for k in ("references", "candidate_misses", "min_misses", "headroom",
                                        "headroom_no_bypass", "decisions", "inconsistent_fills")}
        parent = near_opt / f"{stem}.json" if near_opt else None
        if parent is not None and parent.exists():
            try:
                out["opt_vs_closest"] = opt_tool.compare(r, json.loads(parent.read_text()))
            except ValueError as exc:
                out["opt_vs_closest"] = {"error": str(exc)[:300]}
    else:
        out["opt"] = {"error": (done.stderr or done.stdout)[-300:]}
    log.unlink(missing_ok=True)
    return stem, out


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def why_failed(rec: dict) -> str:
    lines = [f"Candidate {rec.get('id', '?')} FAILED at {rec['status']}; it was not scored.",
             f"Reason: {rec['error']}"]
    for stem, tail in (rec.get("run_failures") or {}).items():
        lines.append(f"--- {stem}, end of its output ---\n{tail}")
    return "\n".join(lines)[:WHY_MAX]


def why_ok(rec: dict) -> str:
    near, cmp_ = rec.get("closest") or {}, rec.get("comparison") or {}
    lines = [f"Candidate {rec['id']}: score {rec['score']:.5f}, the geometric mean over {len(rec['vs_mj'])} traces "
             f"of its IPC over Mockingjay's on the same trace ({RUN_WARMUP // 10**6}M warm-up + "
             f"{RUN_SIM // 10**6}M measured); Mockingjay, the seed and the design to beat, scores 1.0: "
             f"{'ahead' if rec['score'] > 1 else 'behind' if rec['score'] < 1 else 'level'}."]
    if near:
        change = cmp_.get("score_change")
        lines.append(f"Closest earlier design: {near['id']} (similarity {near['similarity']:.3f}"
                     + (f", score {cmp_['score']:.5f}, change {change:+.5f}" if change is not None else ", not scored yet")
                     + f"). Parts changed: {', '.join(rec.get('touched') or []) or 'none'}.")
    words = rec.get("new_words") or []
    lines.append("New words: " + (", ".join(f"{w['name']} ({w['place']})" for w in words) or "none") + ".")
    lines += ["",
              "Per trace. vs MJ: IPC over Mockingjay's on that trace. change: minus the closest design's.",
              "MPKI: LLC misses per 1000 instructions. memory: share of all cycles stalled on the memory",
              "hierarchy at any level; LLC: the part of it stalled on LLC hits; DRAM: the part stalled on",
              "DRAM. retire: share of all cycles spent retiring instructions, which no replacement policy",
              "changes. headroom: share of the sampled misses Belady's MIN avoids on this candidate's own",
              "stream. Then the sampled fills judged against MIN: kept a dead line, evicted a line reused",
              "sooner, should have bypassed, bypassed wrongly (change from the closest design in brackets).",
              f"{'trace':<24}{'vs MJ':>8}{'change':>9}{'MPKI':>7}{'memory':>8}{'LLC':>6}{'DRAM':>7}{'retire':>8}"
              f"{'headroom':>9}  "
              f"{'kept_dead':>14}{'evicted_sooner':>16}{'should_bypass':>15}{'wrong_bypass':>15}"]
    totals = {k: [0, 0] for k in MISTAKES}
    for stem in sorted(rec["vs_mj"]):
        s = rec["signals"].get(stem, {})
        prof, o, v = s.get("profile", {}), s.get("opt", {}), s.get("opt_vs_closest") or {}
        vd = (v.get("delta") or {}).get("decisions", {}) if v.get("comparable") else {}
        change = (cmp_.get("vs_mj_change") or {}).get(stem)
        cells = []
        for k in MISTAKES:
            n = (o.get("decisions") or {}).get(k)
            d = vd.get(k)
            totals[k][0] += n or 0
            totals[k][1] += d or 0
            cells.append("-" if n is None else f"{n}" + (f" ({d:+d})" if d is not None else ""))
        mpki = prof.get("llc_mpki")
        cause, level = prof.get("cycles_by_cause") or {}, prof.get("memory_by_level") or {}
        lines.append(f"{stem:<24}{rec['vs_mj'][stem]:>8.4f}"
                     f"{'-' if change is None else f'{change:+.4f}':>9}"
                     f"{'-' if mpki is None else f'{mpki:.2f}':>7}{_pct(cause.get('backend_memory')):>8}"
                     f"{_pct(level.get('llc')):>6}{_pct(prof.get('dram_share')):>7}{_pct(cause.get('retiring')):>8}"
                     f"{_pct(o.get('headroom')):>9}  {cells[0]:>14}{cells[1]:>16}{cells[2]:>15}{cells[3]:>15}")
        if o.get("decisions", {}).get("compared") == 0:
            lines.append(f"{'':<24}(OPT judged no fills on this trace: no signal)")
    biggest = max(MISTAKES, key=lambda k: totals[k][0])
    lines += ["", "All traces, sampled fills: " + ", ".join(
        f"{k} {totals[k][0]}" + (f" ({totals[k][1]:+d})" if cmp_ else "") for k in MISTAKES)
        + f". The largest kind of mistake is {biggest}. Mistakes of every kind are addressed by changing what "
          "the policy remembers or how it decides, not by moving one threshold."]
    if "metadata_kb" in rec.get("cost", {}):
        c = rec["cost"]
        lines.append(f"Metadata: {c['metadata_kb']:.2f} KB SRAM, {c['metadata_area_mm2']:.4f} mm2 at "
                     f"{c['cacti_node_nm']} nm (reported, not scored).")
    return "\n".join(lines)[:WHY_MAX]


def append_record(rec: dict) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    fd = os.open(RUN / "records.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with open(RECORDS, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        tallies: dict[str, dict] = {}
        for r in map(json.loads, filter(str.strip, RECORDS.read_text().splitlines())):
            for w in r.get("new_words") or []:
                t = tallies.setdefault(w["name"], {"used": 0, "failed": 0, "better": 0, "worse": 0, "same": 0,
                                                   "unknown": 0})
                t["used"] += 1
                change = (r.get("comparison") or {}).get("score_change")
                t["failed" if r["status"] != "ok" else "unknown" if change is None else
                  "better" if change > 0 else "worse" if change < 0 else "same"] += 1
        _write_atomic(RUN / "word_tallies.json", json.dumps(tallies, indent=1, sort_keys=True))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Failed(Exception):
    def __init__(self, status: str, error: str):
        super().__init__(error)
        self.status = status


def vs_mockingjay(ipcs: dict, reference: dict) -> tuple[float, dict]:
    ratios = {s: ipcs[s] / reference[s] for s in TRACES}
    return math.exp(sum(math.log(r) for r in ratios.values()) / len(ratios)), ratios


def mockingjay_ipcs(did: str, sid: str, seed: dict) -> dict | None:
    if did == sid:
        return None
    mj = seed["result"]
    if not mj or mj.get("lengths") != [RUN_WARMUP, RUN_SIM] or mj.get("traces") != TRACES \
            or not all(mj.get("ipc", {}).get(s) for s in TRACES):
        raise Failed("no_seed_result", "Mockingjay, the seed, has no IPC at these lengths and traces yet: "
                                       "every score is against its IPC on each trace, so it is scored first")
    return mj["ipc"]


def _evaluate(text: str, rec: dict, work: Path) -> None:
    t = time.time()
    parts = split_candidate(text)
    missing = [s for s in (STRUCTURE_DESCRIPTION, NEW_WORDS) if s not in parts]
    if missing or not parts[STRUCTURE_DESCRIPTION].strip():
        raise Failed("malformed", f"candidate is missing section(s): {', '.join(missing or [STRUCTURE_DESCRIPTION])}")
    description, words_text = parts[STRUCTURE_DESCRIPTION] + "\n", parts[NEW_WORDS]
    rec["description"], rec["new_words_text"] = description, words_text
    try:
        size = len(canonical(description, words_text).splitlines())
    except yaml.YAMLError:
        size = None
    if size is not None and size > MAX_LINES:
        raise Failed("too_large", f"the design is {size} canonical lines; this run allows {MAX_LINES} "
                                  f"(three times the seed's): a smaller design, please")

    try:
        declared = new_words.parse(words_text)
        words = new_words.check(declared)
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise Failed("words_refused", str(exc)[:2000]) from exc
    try:
        validate.parse(description, words)
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise Failed("schema_rejected", str(exc)[:2000]) from exc
    rec["new_words"] = [w.definition() for w in declared]
    rec["seconds"]["checks"] = round(time.time() - t, 2)

    gone = absent(TRACES)
    if gone:
        raise Failed("missing_traces", f"{len(gone)} trace(s) not on this machine: {', '.join(gone)}")

    sid = seed_id()
    did = design_id(description, words_text)
    rec["id"] = did
    designs = load_designs()
    seed = next(d for d in designs if d["id"] == sid)
    reference = mockingjay_ipcs(did, sid, seed)
    earlier = scored_before(did, designs)
    if earlier is not None:
        rec.update({k: earlier[k] for k in ("closest", "touched", "change", "change_lines", "score", "vs_mj",
                                            "ipc", "run_seconds", "cost", "signals", "comparison")
                    if k in earlier})
        rec["cpp"], rec["reused_result"] = {"reused": did}, earlier["time"]
        return
    canon = canonical(description, words_text)
    near, similarity = closest(canon, did, designs)
    if near is not None:
        rec["closest"] = {"id": near["id"], "similarity": round(similarity, 4)}
        rec["touched"] = touched(without_doc(yaml.safe_load(near["description"])),
                                 without_doc(yaml.safe_load(description)))
        if yaml.safe_load(near["words"] or "[]") != yaml.safe_load(words_text or "[]"):
            rec["touched"].append("new_words")
        rec["change"] = "".join(difflib.unified_diff(near["canonical"].splitlines(True), canon.splitlines(True),
                                                     near["id"], did, n=2))[:8000]
        rec["change_lines"] = step_size(near["canonical"], canon)
        if MAX_CHANGE and rec["change_lines"] > MAX_CHANGE:
            raise Failed("step_too_large", f"this candidate changes {rec['change_lines']} canonical lines against "
                                           f"the closest earlier design {near['id']}; this run allows {MAX_CHANGE} "
                                           f"per step: make one change at a time")
    rec["declared_kb"] = round(declared_kb(description, words), 3)
    if rec["declared_kb"] > MAX_KB:
        raise Failed("too_much_storage", f"this design declares {rec['declared_kb']:.1f} KB of replacement metadata "
                                         f"at this LLC; the budget is {MAX_KB:g} KB (the seed declares 47.4 KB): "
                                         f"pay for new state by narrowing or removing state elsewhere")
    try:
        new_words.check(declared, REGISTRY)
    except ValueError as exc:
        raise Failed("words_refused", str(exc)[:2000]) from exc
    if declared:
        registered = {json.loads(l)["name"] for l in REGISTRY.read_text().splitlines() if l.strip()}
        assert {w.name for w in declared} <= registered, "an accepted word is missing from the registry"
    (work / "description.yaml").write_text(description)

    t = time.time()
    header, source, built, why = write_cpp(description, words_text, did, near, seed, work, rec)
    rec["seconds"]["cpp"] = round(time.time() - t, 1)
    if built is None:
        raise Failed("cpp_failed", why)
    assert built.success and built.binary and built.source_id, "a build reported success without a binary or a source id"
    (work / "config.json").write_text(json.dumps(built.config))

    t = time.time()
    runs = run_traces(built.binary, TRACES, opt_log_dir=work, tag="run", warmup=RUN_WARMUP, sim=RUN_SIM)
    rec["seconds"]["runs"] = round(time.time() - t, 1)
    assert set(runs) == set(TRACES), f"runs for {sorted(runs)}, asked for {TRACES}"
    rec["run_seconds"] = {s: round(r.wall_s, 1) for s, r in runs.items()}
    ipcs = {s: (r.ipc if r.success else None) for s, r in runs.items()}
    rec["ipc"] = ipcs
    bad = sorted(s for s, v in ipcs.items() if not v)
    if bad:
        rec["run_failures"] = {s: runs[s].stdout_tail[-800:] for s in bad}
        raise Failed("incomplete", f"{len(bad)} of {len(TRACES)} traces did not finish: {', '.join(bad)}")

    combined, ratios = vs_mockingjay(ipcs, reference or ipcs)
    rec.update(score=combined, vs_mj=ratios)
    t = time.time()
    try:
        rec["cost"] = cacti_metrics(description, built.config, runs, words=words)
    except CactiError as exc:
        rec["cost"] = {"cacti_error": str(exc)[:500]}
    (work / "stats").mkdir(exist_ok=True)
    (work / "opt").mkdir(exist_ok=True)
    near_opt = near["dir"] / "opt" if near and near["result"] and \
        near["result"].get("lengths") == [RUN_WARMUP, RUN_SIM] else None
    with ThreadPoolExecutor(max_workers=min(8, len(TRACES))) as pool:
        rec["signals"] = dict(pool.map(lambda s: trace_signals(s, runs[s], work, words, near_opt), TRACES))
    rec["seconds"]["signals"] = round(time.time() - t, 1)

    if near_opt is not None:
        nr = near["result"]
        rec["comparison"] = {"id": near["id"], "score": nr["score"], "score_change": combined - nr["score"],
                             "vs_mj_change": {s: ratios[s] - nr["vs_mj"][s] for s in ratios
                                              if (nr.get("vs_mj") or {}).get(s) is not None}}
    add_to_store(did, description, words_text, header, source,
                 {"score": combined, "vs_mj": ratios, "ipc": ipcs, "lengths": [RUN_WARMUP, RUN_SIM],
                  "traces": TRACES}, work / "opt")


def evaluate(program_path: str):
    t0 = time.time()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    work = RUN / "candidates" / f"{stamp}_{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    text = Path(program_path).read_text()
    (work / "candidate.txt").write_text(text)
    rec = {"time": stamp, "work": str(work), "lengths": [RUN_WARMUP, RUN_SIM], "traces": TRACES,
           "cpp_model": CPP_MODEL, "cpp_calls": [], "seconds": {}}
    try:
        _evaluate(text, rec, work)
        rec["status"] = "ok"
        metrics = {"combined_score": rec["score"], "status": "ok",
                   **{k: v for k, v in rec["cost"].items() if isinstance(v, (int, float))}}
        why = why_ok(rec)
        assert metrics["combined_score"] == rec["score"] > 0 and set(rec["vs_mj"]) == set(TRACES), \
            "the score returned is not the record's, or a trace has no ratio"
        assert len(why) <= WHY_MAX, "the why text exceeds OpenEvolve's artifact cap"
    except Failed as exc:
        rec.update(status=exc.status, error=str(exc), score=None)
        metrics, why = {"combined_score": 0.0, "status": exc.status, "error": str(exc)[:2000]}, why_failed(rec)
    except Exception as exc:
        rec.update(status="error", error=f"{type(exc).__name__}: {exc}"[:2000], score=None,
                   traceback=traceback.format_exc()[-4000:])
        metrics, why = {"combined_score": 0.0, "status": "error", "error": rec["error"]}, why_failed(rec)
    rec["tokens"] = {k: sum((c.get("usage") or {}).get(k, 0) or 0 for c in rec["cpp_calls"])
                     for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for log in work.glob("*.opt.log"):
        log.unlink()
    rec["seconds"]["total"] = round(time.time() - t0, 1)
    (work / "why.txt").write_text(why)
    append_record(rec)
    return as_result(metrics, why)


def as_result(metrics: dict, why: str):
    driver = os.environ.get("A3_RESULT", "")
    order, key = {"skydiscover": (("skydiscover.evaluation.evaluation_result",
                                   "openevolve.evaluation_result"), "feedback")}.get(
        driver, (("openevolve.evaluation_result",
                  "skydiscover.evaluation.evaluation_result"), "why"))
    for module in order:
        try:
            cls = __import__(module, fromlist=["EvaluationResult"]).EvaluationResult
        except ImportError:
            continue
        return cls(metrics=metrics, artifacts={key: why})
    return dict(metrics)


if __name__ == "__main__":
    res = evaluate(sys.argv[1])
    m, a = (res.metrics, res.artifacts) if hasattr(res, "metrics") else (res["metrics"], res["artifacts"])
    print(json.dumps(m, indent=1, default=str))
    print(a["why"])
