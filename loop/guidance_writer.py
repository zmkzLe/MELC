from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from evolver_prompt import GUIDANCE_MAX

MISTAKES = ("kept_dead", "evicted_sooner", "should_bypass", "wrong_bypass")
MEANS = {"kept_dead": "kept a line never reused again while evicting one that was",
         "evicted_sooner": "evicted the line reused soonest",
         "should_bypass": "inserted a line never reused before it was evicted",
         "wrong_bypass": "bypassed a line reused before every resident line"}
PARAGRAPH_WORDS = 250
WORDS_CHARS, REFUSALS_CHARS, PARAGRAPH_CHARS = 4000, 1400, 2000
SUMMARY_WORDS = 25
LLM = {"max_tokens": 12_000, "timeout": 300, "retries": 1}

ADVISOR = f"""\
You advise an agent that evolves last-level-cache replacement policies for
ChampSim (the DPC4 single-core LLC: 4096 sets, 12 ways, no prefetcher) as
structure descriptions in a fixed vocabulary plus new words it may declare. Its
score is, on each training trace, the design's IPC over Mockingjay's IPC on the
same trace, as a geometric mean over the traces: the seed, Mockingjay, scores 1.0
and above 1.0 beats it. For its parent design it sees per-trace IPC, LLC MPKI, the
OPT headroom (the share of sampled misses Belady's MIN would avoid) and four
kinds of mistake judged against MIN: {'; '.join(f'{k}: {v}' for k, v in MEANS.items())}.
Below are the facts of the run so far. Write the guidance for its next proposals
as one paragraph of at most {PARAGRAPH_WORDS} words of plain prose: no heading, no
list, no format or syntax rules. Every sentence follows from the facts; name the
traces, the parts of the design and the mistake classes you mean, and invent no
number. Say which kinds of change paid off and which lost; the largest remaining
mistake on the traces with the most headroom and what kind of change (what the
policy remembers, how it decides) would address it; what to stop doing; and where
a new word would express something the vocabulary cannot. Name no paper,
published policy or technique from outside this run: draw only on these facts
and on the five designs the agent is shown (LRU, SRRIP, DRRIP, SHiP and the
seed, Mockingjay). Reply with the paragraph alone."""


def iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _short(text: str, n: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def clean_error(error: str, n: int = 240) -> str:
    t = re.sub(r"\d+ validation errors? for \w+", "", error or "")
    t = re.sub(r"For further information visit \S+", "", t)
    t = re.sub(r"\[type=[^,\]]*, input_value=(.*?), input_type=[^\]]*\]",
               lambda m: f"(written: {_short(m.group(1), 80)})", t)
    t = re.sub(r"\[type=[^\]]*\]", "", t)
    return _short(t, n)


def refusals(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in records:
        status = r.get("status")
        if status in (None, "ok"):
            continue
        e = out.setdefault(status, {"count": 0, "reasons": []})
        e["count"] += 1
        reason = clean_error(r.get("error"))
        if reason and reason not in e["reasons"]:
            e["reasons"].append(reason)
    return out


def error_kind(error: str) -> str:
    t = _short(error, 600)
    t = re.sub(r"\d+ validation errors? for \w+", "", t)
    t = re.sub(r"\[type=[^\]]*\]", "", t)
    t = re.sub(r"For further information visit \S+", "", t)
    t = re.sub(r"\bat line \d+, column \d+", "", t)
    t = re.sub(r"'[^']*'", "'…'", t)
    t = re.sub(r"^[a-z][a-z0-9_]*: ", "…: ", t)
    t = re.sub(r"\b[0-9a-f]{12}\b", "…", t)
    t = re.sub(r"\d+", "N", t)
    return re.sub(r"\s+", " ", t).strip()[:160]


def recurring(records: list[dict], top: int = 5, at_least: int = 2) -> list[tuple[int, str, str]]:
    kinds: dict[str, list] = {}
    for r in records:
        if r.get("status") in (None, "ok"):
            continue
        e = kinds.setdefault(error_kind(r.get("error") or ""), [0, r["status"], clean_error(r.get("error"))])
        e[0] += 1
    return sorted(((n, s, ex) for n, s, ex in kinds.values() if n >= at_least), reverse=True)[:top]


def parts(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in records:
        change = (r.get("comparison") or {}).get("score_change")
        if r.get("status") != "ok" or r.get("reused_result") or change is None:
            continue
        for p in r.get("touched") or []:
            if p == "name":
                continue
            e = out.setdefault(p, {"tried": 0, "improved": 0, "changes": []})
            e["tried"] += 1
            e["improved"] += change > 0
            e["changes"].append(change)
    return out


def scored(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("status") == "ok" and isinstance(r.get("score"), (int, float))]


def best(records: list[dict]) -> dict | None:
    ok = scored(records)
    return max(ok, key=lambda r: r["score"]) if ok else None


def vs_mj(rec: dict) -> dict:
    return rec.get("vs_mj") or (rec.get("vs_seed") or {}).get("ipc") or {}


def headroom_rows(rec: dict, n: int = 6) -> list[str]:
    rows = []
    for stem, s in (rec.get("signals") or {}).items():
        o, d = s.get("opt") or {}, (s.get("opt") or {}).get("decisions") or {}
        if o.get("headroom") is None or not d.get("compared"):
            continue
        big = max(MISTAKES, key=lambda k: d.get(k) or 0)
        mpki = (s.get("profile") or {}).get("llc_mpki")
        rows.append((o["headroom"], f"{stem}: headroom {100 * o['headroom']:.1f}%, largest mistake {big} "
                                    f"({d.get(big) or 0} of {d['compared']} sampled fills), IPC over Mockingjay's "
                                    f"{vs_mj(rec).get(stem, float('nan')):.4f}"
                                    + (f", LLC MPKI {mpki:.1f}" if isinstance(mpki, (int, float)) else "")))
    rows.sort(key=lambda x: -x[0])
    return [text for _, text in rows[:n]]


def candidate_line(i: int, r: dict) -> str:
    parts_ = ", ".join(p for p in (r.get("touched") or []) if p != "name")
    words = ", ".join(w["name"] for w in r.get("new_words") or [])
    what = f"changed {r['change_lines']} canonical lines in {parts_ or 'nothing'}" if r.get("change_lines") is not None else ""
    what += f"{', ' if what else ''}new words {words}" if words else ""
    if r.get("status") != "ok":
        return f"{i}. refused ({r.get('status')})" + (f", {what}" if what else "") + f": {clean_error(r.get('error'), 200)}"
    if not r.get("closest"):
        return f"{i}. the seed, scored {r['score']:.5f}"
    change = (r.get("comparison") or {}).get("score_change")
    tail = ("a repeat of an earlier design" if r.get("reused_result") else "no closest design scored" if change is None
            else f"{change:+.5f} against its closest earlier design")
    return f"{i}. scored {r.get('id')} at {r['score']:.5f}, {what}: {tail}"


def summary(records: list[dict], registry: list[dict], tallies: dict, every: int) -> str:
    n, ok, seed = len(records), scored(records), next((r for r in records if r.get("status") == "ok"), None)
    top = best(records)
    ref = refusals(records)
    lines = [f"Run so far: {n} candidates, {len(ok)} scored, {n - len(ok)} refused or failed"
             + (" (" + ", ".join(f"{k} {v['count']}" for k, v in sorted(ref.items())) + ")" if ref else "") + "."]
    rep = recurring(records)
    if rep:
        lines.append("Refusal kinds seen more than once this run (a pattern, not a one-off): "
                     + "; ".join(f"{c} times, {s}: {ex}" for c, s, ex in rep))
    every_kind = recurring(records, top=12, at_least=1)
    if every_kind:
        lines += ["Every refusal reason this run, one example per kind with its count (kinds are grouped by the "
                  "message with names and numbers removed; merge any that are the same mistake):"]
        lines += [f"- {c} x {s}: {ex}" for c, s, ex in every_kind]
    if seed:
        lines.append(f"The seed scored {seed['score']:.5f}.")
    if top:
        lines.append(f"Best so far: {top['id']} at {top['score']:.5f}"
                     + (f", {100 * (top['score'] / seed['score'] - 1):+.2f}% over Mockingjay" if seed else "") + ".")
    p = parts(records)
    if p:
        lines += ["", "Parts of the design changed so far (candidates that changed it, how many beat their closest "
                      "earlier design, best and mean change of the score):"]
        for name, e in sorted(p.items(), key=lambda kv: (-kv[1]["improved"], -kv[1]["tried"])):
            lines.append(f"- {name}: {e['tried']} tried, {e['improved']} improved, best {max(e['changes']):+.5f}, "
                         f"mean {sum(e['changes']) / len(e['changes']):+.5f}")
    last = records[-every:]
    lines += ["", f"The last {len(last)} candidates, oldest first:"]
    lines += [candidate_line(i, r) for i, r in enumerate(last, n - len(last) + 1)]
    if top:
        rows = headroom_rows(top)
        if rows:
            lines += ["", f"The best design's traces with the most headroom, and its largest mistake on each:"]
            lines += [f"- {row}" for row in rows]
    lines += ["", "New words accepted this run" + (", best-scoring first:" if registry else ": none yet.")]
    results = word_results(records)
    ranked = sorted(registry, key=lambda w: -max(results.get(w["name"], [float("-inf")])))
    for w in ranked[:SUMMARY_WORDS]:
        t = tallies.get(w["name"]) or {}
        lines.append(f"- {w['name']} ({w['place']}): {_short(w.get('means'), 200)}"
                     + (f" [used {t.get('used', 0)}, better {t.get('better', 0)}, worse {t.get('worse', 0)}, "
                        f"same {t.get('same', 0)}, failed {t.get('failed', 0)}]" if t else ""))
    if len(ranked) > SUMMARY_WORDS:
        lines.append(f"- and {len(ranked) - SUMMARY_WORDS} more with lower or no scores: "
                     + _short(", ".join(w["name"] for w in ranked[SUMMARY_WORDS:]), 400))
    return "\n".join(lines)


def word_results(records: list[dict]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in scored(records):
        if r.get("reused_result"):
            continue
        for w in r.get("new_words") or []:
            out.setdefault(w["name"], []).append(r["score"])
    return out


def words_text(registry: list[dict], records: list[dict]) -> str:
    if not registry:
        return "New words accepted this run: none yet. A new word's name must be one no design of this run has used."
    seed = next((r["score"] for r in scored(records)), None)
    results = word_results(records)
    tried = sorted((w for w in registry if w["name"] in results), key=lambda w: -max(results[w["name"]]))
    untried = [w["name"] for w in registry if w["name"] not in results]
    head = ("New words of scored designs this run, best first, each with the declaration to repeat verbatim to reuse "
            "it and how the designs using it scored (the same name with another definition is refused; for another "
            "meaning choose a name none of these use):" if tried else
            "No scored design has used a new word yet.")
    items, used = [], len(head)
    for i, w in enumerate(tried):
        s = results[w["name"]]
        note = f"# used by {len(s)} scored design(s); best {max(s):.5f}" + (
            f" ({100 * (max(s) / seed - 1):+.2f}% against the seed)" if seed else "")
        item = note + "\n" + yaml.safe_dump([{k: w[k] for k in ("name", "place", "params", "means") if k in w}],
                                            sort_keys=False, default_flow_style=None, width=100).rstrip()
        if used + len(item) + 1 > WORDS_CHARS:
            untried = [w2["name"] for w2 in tried[i:]] + untried
            break
        items.append(item)
        used += len(item) + 1
    if untried:
        items.append("Names also taken (declared by a design not scored yet, or not shown above): "
                     + ", ".join(untried)[:300])
    return head + "\n" + "\n".join(items)


def refusals_text(records: list[dict], every: int) -> str:
    ref = refusals(records[-every:])
    if not ref:
        text = f"Refused among the last {min(every, len(records))} candidates: none."
    else:
        parts_ = [f"{k} {v['count']} (" + "; ".join(v["reasons"][:3]) + ")" for k, v in sorted(ref.items())]
        text = f"Refused among the last {min(every, len(records))} candidates, and why: " + ". ".join(parts_)
    recent = records[-3 * every:]
    rep = recurring(recent)
    if rep:
        text += (f"\nRefusal kinds seen more than once among the last {len(recent)} candidates, a pattern to stop: "
                 + "; ".join(f"{c} times ({s}): {ex}" for c, s, ex in rep))
    return text[:REFUSALS_CHARS]


def clean_paragraph(reply: str) -> str:
    text = re.sub(r"^```.*$", "", reply, flags=re.M).strip().strip("`\"'").strip()
    words = text.split()
    if len(words) > PARAGRAPH_WORDS + 50:
        text = " ".join(words[:PARAGRAPH_WORDS + 50]) + " [cut]"
    return text[:PARAGRAPH_CHARS]


def compose(version: int, records: list[dict], registry: list[dict], every: int, paragraph: str) -> str:
    text = "\n\n".join([f"Guidance version {version}, written after {len(records)} candidates.",
                        words_text(registry, records), refusals_text(records, every),
                        paragraph.strip() or "(no guidance paragraph yet)"])
    assert len(text) <= GUIDANCE_MAX, f"guidance of {len(text)} characters exceeds the cap {GUIDANCE_MAX}"
    return text


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def ask(summary_text: str, previous: str, model: str, stub: Path | None) -> tuple[str, dict, float]:
    user = summary_text + ("\n\nThe previous guidance paragraph, to rewrite:\n" + previous if previous else "")
    msgs = [{"role": "system", "content": ADVISOR}, {"role": "user", "content": user}]
    t0 = time.time()
    if stub is not None:
        if not stub.exists():
            raise RuntimeError(f"stub {stub} missing: a failed call")
        return stub.read_text(), {"stub": True}, round(time.time() - t0, 1)
    import cpp_writer
    reply, usage = cpp_writer.call(msgs, model, **LLM)
    return reply, usage, round(time.time() - t0, 1)


def write_version(a: argparse.Namespace, records: list[dict], log: list[dict]) -> None:
    version = len(log) + 1
    registry = read_jsonl(a.run_dir / "new_words_registry.jsonl")
    tallies = json.loads((a.run_dir / "word_tallies.json").read_text()) if (a.run_dir / "word_tallies.json").exists() else {}
    previous = next((e["paragraph"] for e in reversed(log) if e.get("paragraph")), "")
    facts = summary(records, registry, tallies, a.every)
    keep = a.out.parent / "guidance"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / f"{version}_summary.txt").write_text(facts)
    line = {"version": version, "time": iso(), "records": len(records), "model": a.model}
    try:
        reply, usage, seconds = ask(facts, previous, a.model, a.stub)
        (keep / f"{version}_reply.txt").write_text(reply)
        paragraph = clean_paragraph(reply)
        line.update(usage=usage, seconds=seconds, paragraph=paragraph)
    except Exception as exc:
        paragraph = previous
        line.update(error=f"{type(exc).__name__}: {exc}"[:500], paragraph=paragraph, kept_previous=True)
    text = compose(version, records, registry, a.every, paragraph)
    (keep / f"{version}.txt").write_text(text)
    write_atomic(a.out, text)
    line["chars"] = len(text)
    with open(a.log, "a") as fh:
        fh.write(json.dumps(line) + "\n")
    print(f"[{iso()}] version {version} after {len(records)} candidates: {len(text)} chars"
          + (f", FAILED call, previous paragraph kept: {line['error']}" if "error" in line else
             f", {line.get('usage', {}).get('total_tokens', '?')} tokens in {line.get('seconds')} s"), flush=True)


def alive(pid: int | None) -> bool:
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="the evaluator's run directory (records.jsonl, the registry)")
    ap.add_argument("--out", required=True, help="the guidance file the driver reads (--guidance)")
    ap.add_argument("--every", type=int, default=10, help="candidates between rewrites")
    ap.add_argument("--model", default="gemini-2.5-pro")
    ap.add_argument("--poll", type=float, default=60)
    ap.add_argument("--search-pid", type=int, default=None, help="exit when this process is gone")
    ap.add_argument("--once", action="store_true", help="write a version if one is due, then exit")
    ap.add_argument("--stub", type=Path, default=None, help="testing: the reply is this file's text; missing = a failed call")
    a = ap.parse_args()
    a.run_dir, a.out = Path(a.run_dir).expanduser().resolve(), Path(a.out).expanduser().resolve()
    a.log = a.out.parent / "guidance.jsonl"
    assert a.run_dir.is_dir(), f"{a.run_dir}: no such run directory"
    assert a.every > 0
    print(f"guidance writer: {a.run_dir} -> {a.out}, every {a.every} candidates, model {a.model}, "
          f"search pid {a.search_pid}, stub {a.stub}", flush=True)
    while True:
        records = read_jsonl(a.run_dir / "records.jsonl")
        log = read_jsonl(a.log)
        due = len(records) >= (log[-1]["records"] + a.every if log else a.every)
        if due:
            write_version(a, records, log)
        if a.once:
            break
        if not alive(a.search_pid):
            print(f"[{iso()}] the search is gone: done", flush=True)
            break
        time.sleep(a.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
