"""Write a policy's ChampSim C++ from its structure description (CHIA hackathon)."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from candidate import CHAMPSIM, HEADER, MARKER, SOURCE
from run_openevolve import vertex_base, vertex_credentials

MEANINGS = CHAMPSIM / "structure_description" / "meanings.yaml"
EXAMPLE_YAML = CHAMPSIM / "structure_description" / "policies" / "lru.yaml"
EXAMPLE_CODE = CHAMPSIM / "replacement" / "lru"

RULES = f"""\
You translate one cache replacement policy, given as a structure description
(YAML), into a ChampSim replacement module in C++17.

Faithfulness is the whole task.
- Implement exactly what the description says, as the meanings below define each
  word. Add nothing: no state, table, counter, constant, heuristic or behaviour
  the description does not state. Leave nothing out.
- Keep only the storage the description declares, at the declared sizes: a
  per_line field has NUM_SET * NUM_WAY copies, per_set NUM_SET, per_cpu NUM_CPUS,
  global one; a table has `entries` entries (one table per core if per_cpu); a
  sampler as its meaning says. Implement each field's arithmetic (wrap, clamp,
  signed) exactly at its declared width.
- Follow the order of events and rules, `when`, `stop` and `free` exactly as the
  meanings define them.
- `doc` and YAML comments are not part of the policy. Where they differ from the
  keys, the keys win.

ChampSim's interface, as in the worked example:
- The description's `name` is not the class name. The class is named @MODULE@,
  literally, wherever a class name appears: `class @MODULE@ :
  public champsim::modules::replacement`, a constructor `explicit
  @MODULE@(CACHE* cache);` that initialises `replacement(cache)`, the header guard
  REPLACEMENT_@MODULE@_H, and the source's `#include "@MODULE@.h"`.
- The header includes "cache.h" and "modules.h". Declare `long NUM_SET;` and
  `long NUM_WAY;` as members and set them from cache->NUM_SET and cache->NUM_WAY
  in the constructor's initialiser list, as the example does; `cache` exists only
  in the constructor. NUM_CPUS and LOG2_BLOCK_SIZE are
  ChampSim constants; include "champsim.h" in the source for NUM_CPUS.
  champsim::lg2 is in "msl/bits.h".
- The three hooks have exactly the example's signatures: find_victim,
  update_replacement_state, replacement_cache_fill. Declare nothing else public.
  No `override` and no `virtual`: ChampSim finds the hooks by name, and the base
  class declares none of them.
- A hook does only what the description's events and victim say. An event the
  description does not list does nothing: nothing is implied or run twice.
- Where a meaning fixes an order, a tie-break or a bit width, follow it to the
  letter, even where another choice looks equivalent.
- ip and full_addr are champsim::address; read them with .to<uint64_t>().
  full_addr keeps its bit positions with the 6 offset bits zeroed, so the line
  address is full_addr >> LOG2_BLOCK_SIZE.
- Access types are access_type::LOAD, RFO, PREFETCH, WRITE and TRANSLATION.
- find_victim returns a way in 0..NUM_WAY-1, or NUM_WAY to bypass, and never
  NUM_WAY for access_type::WRITE.
- `way` may equal NUM_WAY in update_replacement_state (a miss) and in
  replacement_cache_fill (a bypassed fill): never index per_line storage with it.
- Print nothing. No randomness, no static or global mutable state.

Reply with one fenced code block holding exactly these two sections:
{MARKER.format(HEADER)}
<the header>
{MARKER.format(SOURCE)}
<the source>
"""


def messages(description: str, new_words: str = "") -> list[dict]:
    example = (f"--- lru.yaml ---\n{EXAMPLE_YAML.read_text()}\n"
               f"--- lru.h ---\n{(EXAMPLE_CODE / 'lru.h').read_text()}\n"
               f"--- lru.cc ---\n{(EXAMPLE_CODE / 'lru.cc').read_text()}")
    user = (f"== The machine, and the meaning of every word ==\n{MEANINGS.read_text()}\n"
            f"== New words this description declares ==\n{new_words.strip() or '(none)'}\n\n"
            f"== Worked example: LRU's structure description and its ChampSim module "
            f"(named lru there; yours is @MODULE@) ==\n{example}\n"
            f"== Translate this structure description ==\n{description}")
    return [{"role": "system", "content": RULES}, {"role": "user", "content": user}]


EDIT = """\
Above is an earlier design: its structure description, its new words, and the C++
that implements it, which builds and runs every trace. Edit that C++ so it
implements the new structure description exactly, as the meanings define each
word. Change what the difference requires and nothing else; keep the rest of the
code as it is. Reply with both sections complete."""


def _change(description: str, new_words: str, base_description: str, base_words: str) -> str:
    before = f"{base_description.rstrip()}\n# new words\n{base_words.strip() or '(none)'}\n"
    after = f"{description.rstrip()}\n# new words\n{new_words.strip() or '(none)'}\n"
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "earlier", "new", n=3))


def _edit_context(description: str, new_words: str, base_description: str, base_words: str,
                  base_header: str, base_source: str) -> str:
    diff = _change(description, new_words, base_description, base_words)
    return (f"== The machine, and the meaning of every word ==\n{MEANINGS.read_text()}\n"
            f"== The earlier design's structure description ==\n{base_description}\n"
            f"== The earlier design's new words ==\n{base_words.strip() or '(none)'}\n\n"
            f"== The earlier design's C++ (class @MODULE@) ==\n{MARKER.format(HEADER)}\n{base_header}\n"
            f"{MARKER.format(SOURCE)}\n{base_source}\n"
            f"== The change, as a diff of the structure description and new words ==\n{diff or '(none)'}\n"
            f"== The new structure description ==\n{description}\n"
            f"== The new words it declares ==\n{new_words.strip() or '(none)'}\n\n")


def edit_messages(description: str, new_words: str, base_description: str, base_words: str,
                  base_header: str, base_source: str) -> list[dict]:
    user = _edit_context(description, new_words, base_description, base_words, base_header, base_source) + EDIT
    return [{"role": "system", "content": RULES}, {"role": "user", "content": user}]


AUDIT = """\
Above are the earlier design, the change in its structure description, the new
structure description, and the C++ an editor produced for it, followed by the
difference between the earlier and the edited C++. Some of that difference may
go beyond what the change in the description requires: an editor once rewrote a
tie-break, dropped a counter's wrap and changed which sampler entry is the victim
for a change that touched one threshold. Return the edited C++ with every change
the description's change does not require reverted to the earlier code, and
every change it does require kept, as the meanings define each word. Add nothing
new. Reply with both sections complete."""


def code_lines(text: str) -> list[str]:
    return [s for s in (line.strip() for line in text.splitlines()) if s and not s.startswith("//")]


def changed_lines(base_header: str, base_source: str, header: str, source: str) -> int:
    n = 0
    for a, b in ((base_header, header), (base_source, source)):
        for line in difflib.unified_diff(code_lines(a), code_lines(b), n=0, lineterm=""):
            if line[:1] in "+-" and not line.startswith(("+++", "---")):
                n += 1
    return n


def code_diff(base_header: str, base_source: str, header: str, source: str) -> str:
    out = []
    for name, a, b in (("policy.h", base_header, header), ("policy.cc", base_source, source)):
        out += difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"earlier/{name}", f"edited/{name}", n=2)
    return "".join(out)


def audit_messages(description: str, new_words: str, base_description: str, base_words: str,
                   base_header: str, base_source: str, header: str, source: str) -> list[dict]:
    user = (_edit_context(description, new_words, base_description, base_words, base_header, base_source)
            + f"== The edited C++ (class @MODULE@) ==\n{MARKER.format(HEADER)}\n{header}\n"
            f"{MARKER.format(SOURCE)}\n{source}\n"
            f"== The difference between the earlier and the edited C++ ==\n"
            f"{code_diff(base_header, base_source, header, source) or '(none)'}\n\n{AUDIT}")
    return [{"role": "system", "content": RULES}, {"role": "user", "content": user}]


REPAIR = """\
The module you wrote did not compile. The compiler's messages follow; they name
the module's files after the module, which is @MODULE@ in your code.

{errors}

Fix only what these messages require. Change no behaviour: the policy must still
do exactly what the description says, as the meanings define each word. Reply in
the same format, with both sections complete."""


RATE_LIMIT_WAITS = (30, 60, 120, 240)


def call(msgs: list[dict], model: str, max_tokens: int, timeout: int, retries: int) -> tuple[str, dict]:
    attempt, waits = 0, list(RATE_LIMIT_WAITS)
    while True:
        token, project = vertex_credentials()
        body = {"model": model if "/" in model else f"google/{model}", "messages": msgs,
                "max_tokens": max_tokens, "temperature": 0}
        req = urllib.request.Request(f"{vertex_base(project)}/chat/completions", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            reply = json.load(urllib.request.urlopen(req, timeout=timeout))
            usage = reply.get("usage", {})
            if len(waits) < len(RATE_LIMIT_WAITS):
                usage["rate_limit_waits"] = len(RATE_LIMIT_WAITS) - len(waits)
            return reply["choices"][0]["message"].get("content") or "", usage
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429 and waits:
                wait = waits.pop(0)
                print(f"{model}: rate limited (429), trying again in {wait} s", file=sys.stderr)
                time.sleep(wait)
                continue
            if attempt == retries:
                detail = exc.read().decode()[:500] if isinstance(exc, urllib.error.HTTPError) else ""
                raise RuntimeError(f"Gemini call failed after {attempt + 1} attempt(s) and "
                                   f"{len(RATE_LIMIT_WAITS) - len(waits)} rate-limit wait(s): {exc} {detail}") from exc
            attempt += 1


_FENCE = re.compile(r"```[a-zA-Z+]*\n(.*?)```", re.S)
_SECTION = re.compile(rf"^=====\s*({HEADER}|{SOURCE})\s*=====\s*$", re.M)
_CLASS = re.compile(r"\b(?:class|struct)\s+([A-Za-z_@][\w@]*)\s*(?:final\s*)?:\s*public\s+champsim::modules::replacement")


def split(reply: str) -> dict[str, str]:
    m = _FENCE.search(reply)
    text = m.group(1) if m and len(_SECTION.findall(m.group(1))) == 2 else reply
    parts, marks = {}, list(_SECTION.finditer(text))
    for i, mk in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = "\n".join(line for line in text[mk.end():end].splitlines() if not line.startswith("```"))
        parts[mk.group(1)] = body.strip("\n") + "\n"
    missing = [s for s in (HEADER, SOURCE) if not parts.get(s, "").strip()]
    if missing:
        raise ValueError(f"the reply has no {' or '.join(missing)} section")
    declared = _CLASS.search(parts[HEADER])
    if declared is None:
        raise ValueError("the header declares no class derived from champsim::modules::replacement")
    name = declared.group(1)
    if name != "@MODULE@":
        for s in (HEADER, SOURCE):
            parts[s] = re.sub(rf"\b{re.escape(name)}\b", "@MODULE@", parts[s])
    parts[HEADER] = re.sub(r"(\b(?:find_victim|update_replacement_state|replacement_cache_fill|replacement_final_stats)"
                           r"\s*\([^;{]*\))\s*override\b", r"\1", parts[HEADER])
    return parts


class Unusable(ValueError):
    def __init__(self, why: str, got: dict):
        super().__init__(why)
        self.got = got


def _ask(msgs: list[dict], model: str, max_tokens: int, timeout: int, retries: int) -> dict:
    t0 = time.time()
    reply, usage = call(msgs, model, max_tokens, timeout, retries)
    got = {"model": model, "usage": usage, "seconds": round(time.time() - t0, 1), "reply": reply}
    try:
        parts = split(reply)
    except ValueError as exc:
        raise Unusable(str(exc), got) from exc
    return {**got, "header": parts[HEADER], "source": parts[SOURCE]}


def generate(description: str, new_words: str = "", *, model: str = "gemini-2.5-flash",
             max_tokens: int = 32_000, timeout: int = 300, retries: int = 1) -> dict:
    return _ask(messages(description, new_words), model, max_tokens, timeout, retries)


def edit(description: str, new_words: str, base_description: str, base_words: str, base_header: str,
         base_source: str, *, model: str = "gemini-2.5-pro", max_tokens: int = 32_000, timeout: int = 300,
         retries: int = 1) -> dict:
    msgs = edit_messages(description, new_words, base_description, base_words, base_header, base_source)
    return {**_ask(msgs, model, max_tokens, timeout, retries), "messages": msgs}


def audit(description: str, new_words: str, base_description: str, base_words: str, base_header: str,
          base_source: str, header: str, source: str, *, model: str = "gemini-2.5-pro", max_tokens: int = 32_000,
          timeout: int = 300, retries: int = 1) -> dict:
    msgs = audit_messages(description, new_words, base_description, base_words, base_header, base_source,
                          header, source)
    return {**_ask(msgs, model, max_tokens, timeout, retries), "messages": msgs}


def repair_messages(msgs: list[dict], reply: str, errors: str) -> list[dict]:
    return msgs + [{"role": "assistant", "content": reply},
                   {"role": "user", "content": REPAIR.format(errors=errors[-6000:])}]


def repair(description: str, reply: str, errors: str, new_words: str = "", *, model: str = "gemini-2.5-flash",
           max_tokens: int = 32_000, timeout: int = 300, retries: int = 1, msgs: list[dict] | None = None) -> dict:
    msgs = repair_messages(msgs or messages(description, new_words), reply, errors)
    return _ask(msgs, model, max_tokens, timeout, retries)


def save(got: dict, out: Path, prefix: str = "") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{prefix}reply.txt").write_text(got["reply"])
    (out / f"{prefix}call.json").write_text(json.dumps({k: got[k] for k in ("model", "usage", "seconds")}, indent=2))
    if "header" in got:
        (out / "policy.h").write_text(got["header"])
        (out / "policy.cc").write_text(got["source"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yaml", type=Path, required=True)
    ap.add_argument("--words", type=Path, help="the candidate's new words, a YAML list")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--max-tokens", type=int, default=32_000, help="reply ceiling, reasoning included")
    ap.add_argument("--timeout", type=int, default=300, help="seconds allowed for one call")
    ap.add_argument("--retries", type=int, default=1, help="retries of a failed call, each billed")
    ap.add_argument("--reply", type=Path, help="parse a saved reply instead of calling the model")
    args = ap.parse_args()
    if args.reply:
        got = {"model": "saved reply", "usage": {}, "seconds": 0, "reply": args.reply.read_text()}
        parts = split(got["reply"])
        save({**got, "header": parts[HEADER], "source": parts[SOURCE]}, args.out)
        print(f"parsed {args.reply} -> {args.out}")
        return 0
    try:
        got = generate(args.yaml.read_text(), args.words.read_text() if args.words else "", model=args.model,
                       max_tokens=args.max_tokens, timeout=args.timeout, retries=args.retries)
    except Unusable as exc:
        save(exc.got, args.out)
        print(f"{args.model}: unusable reply ({exc}); kept in {args.out / 'reply.txt'}, usage {exc.got['usage']}")
        return 2
    save(got, args.out)
    print(f"{args.model}: {got['seconds']} s, usage {got['usage']} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
