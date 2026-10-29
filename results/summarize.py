import collections, json, math, re, sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent / "runs"
SEED = "c41837327dd0"
RUNS = [("arm_adaevolve_noadapt_g3", "Ada-3.x", "AdaEvolve", "3.1 Pro / 3.8 Flash")]


def described(folder):
    D = Path(folder).expanduser().resolve()
    log = (D / "search.log").read_text(errors="ignore") if (D / "search.log").exists() else ""
    m = re.search(r"effective config: search (\w+)", log)
    search = m.group(1) if m else "openevolve"
    if "guidance: every proposal" in log:
        search += " + guidance"
    models = sorted({c["model"].split("/")[-1] for c in jl(D / "llm_calls.jsonl") if c.get("model")})
    return D, D.name, search, " / ".join(models)


def jl(p):
    return [json.loads(l) for l in open(p) if l.strip()] if Path(p).exists() else []


def gm(xs):
    xs = list(xs)
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def pct(x):
    return f"{(x - 1) * 100:+.2f}%"


def kind(r):
    s, e = r.get("status"), str(r.get("error") or "")
    if s == "words_refused":
        if "already declared" in e: return "new word: redefines a taken name"
        if "must be a YAML list" in e: return "new words: not written as a list"
        if "should match pattern" in e: return "new word: name too long or malformed"
        return "new word: other declaration error"
    if s == "schema_rejected":
        if re.search(r"\nname\n\s+String should match pattern", e): return "design name too long or malformed"
        if "valid dictionary or instance of StructureDescription" in e: return "design section is not YAML (e.g. a copied placeholder)"
        if "never run by any event" in e: return "procedure declared but never run"
        if "not a word of the grammar here" in e: return "a word not allowed there, or not declared"
        if "duplicate key" in e: return "duplicate YAML key"
        if "not a word" in e or "not a declared" in e or "undeclared" in e: return "uses an undeclared word"
        return "other structure error"
    return {"too_much_storage": "over 48 KB of state", "step_too_large": "step too large",
            "too_large": "design too large", "malformed": "reply missing a section",
            "cpp_failed": "C++ failed to build", "no_seed_result": "before the seed was scored"}.get(s, s)


out, runs = [], {}
P = out.append
chosen = [described(a) for a in sys.argv[1:]] or [(BASE / f, l, s, m) for f, l, s, m in RUNS]
for D, label, framework, models in chosen:
    folder = D.name
    recs = jl(D / "yaml_run/records.jsonl")
    calls = jl(D / "llm_calls.jsonl")
    seed = next((r for r in recs if r.get("id") == SEED and r.get("status") == "ok"), None)
    if seed is None:
        print(f"{label}: the seed has not been scored yet; skipped", file=sys.stderr)
        continue
    train = list(seed["ipc"])
    ok = [r for r in recs if r.get("status") == "ok"]
    refused = [r for r in recs if r.get("status") != "ok"]
    vs = {r["id"]: gm(r["ipc"][t] / seed["ipc"][t] for t in train) for r in ok}
    best = max(vs, key=vs.get)
    times = [datetime.strptime(r["time"], "%Y%m%dT%H%M%S") for r in recs]
    ans = [c for c in calls if c.get("status") == 200]
    tk = collections.defaultdict(lambda: [0, 0, 0])
    for c in ans:
        t = tk[c["model"].split("/")[-1]]
        t[0] += 1; t[1] += c.get("prompt_tokens") or 0; t[2] += (c.get("completion_tokens") or 0) + (c.get("reasoning_tokens") or 0)
    cc = [c for r in recs for c in (r.get("cpp_calls") or [])]
    cu = [c.get("usage") or {} for c in cc]
    runs[label] = dict(folder=folder, framework=framework, models=models, recs=recs, ok=ok, refused=refused, vs=vs,
                       best=best, hours=(max(times) - min(times)).total_seconds() / 3600, calls=len(calls),
                       answered=len(ans), tk=dict(tk), cpp_models=sorted({r.get("cpp_model") for r in ok if r.get("cpp_model")}),
                       cpp_calls=len(cc), cpp_in=sum(u.get("prompt_tokens") or 0 for u in cu),
                       cpp_out=sum((u.get("completion_tokens") or 0) + ((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0) for u in cu))

L = list(runs)
P("## Runs\n")
P("| Run | Folder | Search | Proposer models | C++ writer | Hours* | Candidates | Refused | Distinct designs scored | Best on training |")
P("|---|---|---|---|---|---|---|---|---|---|")
for l in L:
    s = runs[l]
    P(f"| {l} | `{s['folder']}` | {s['framework']} | {s['models']} | {', '.join(s['cpp_models'])} | {s['hours']:.1f} | "
      f"{len(s['recs'])} | {len(s['refused'])} | {len(s['vs'])} | {pct(s['vs'][s['best']])} `{s['best']}` |")
P("\n*From the first candidate's start to the last one's.")

P("\n## Refusals by reason\n")
tabs = {l: collections.Counter(kind(r) for r in runs[l]["refused"]) for l in L}
kinds = sorted({k for c in tabs.values() for k in c}, key=lambda k: -sum(c[k] for c in tabs.values()))
P("| Reason | " + " | ".join(L) + " |")
P("|---|" + "---|" * len(L))
for k in kinds:
    P(f"| {k} | " + " | ".join(str(tabs[l][k]) for l in L) + " |")
P("| **total** | " + " | ".join(str(sum(tabs[l].values())) for l in L) + " |")

P("\n## Model calls and tokens\n")
P("| Run | Role | Model | Calls | Input tokens | Output tokens (incl. reasoning) |")
P("|---|---|---|---|---|---|")
for l in L:
    s = runs[l]
    for m, (n, pi, po) in sorted(s["tk"].items()):
        P(f"| {l} | proposer | {m} | {n} | {pi:,} | {po:,} |")
    P(f"| {l} | proposer | (rate-limited, retried) | {s['calls'] - s['answered']} | | |")
    P(f"| {l} | C++ writer | {', '.join(s['cpp_models'])} | {s['cpp_calls']} | {s['cpp_in']:,} | {s['cpp_out']:,} |")

print("\n".join(out))
