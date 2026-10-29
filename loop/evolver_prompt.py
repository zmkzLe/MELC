from __future__ import annotations

import os
from pathlib import Path

import yaml

from candidate import CHAMPSIM, MARKER, POLICIES, STRUCTURE_DESCRIPTION

NEW_WORDS = "NEW_WORDS"
MEANINGS = CHAMPSIM / "structure_description" / "meanings.yaml"


def without_doc(obj):
    if isinstance(obj, dict):
        return {k: without_doc(v) for k, v in obj.items() if k != "doc"}
    if isinstance(obj, list):
        return [without_doc(v) for v in obj]
    return obj


def canonical(description: str, words: str) -> str:
    desc = without_doc(yaml.safe_load(description))
    if isinstance(desc, dict):
        desc.pop("name", None)
    data = {"description": desc, "new_words": yaml.safe_load(words) if words.strip() else []}
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False)


def seed_lines(name: str = "mockingjay") -> int:
    return len(canonical((POLICIES / f"{name}.yaml").read_text(), "").splitlines())


def limits() -> tuple[int, int]:
    return (int(os.environ.get("A3_MAX_CHANGE") or 0),
            int(os.environ.get("A3_MAX_LINES") or 0) or 3 * seed_lines())

RULES = f"""\
You are an expert computer architect, and you design last-level-cache
replacement policies. The codebase is ChampSim, a simulator of an out-of-order
superscalar processor running a program trace, in the DPC4 single-core
configuration: one core, a 3 MB last-level cache of 4096 sets and 12 ways with
64-byte lines and a 35-cycle latency, and no prefetcher anywhere. Your task is
to improve its LLC replacement policy iteratively: each reply of yours is one
design; it is built, run on the training traces and judged, and what was learnt
comes back to you for the next design. The goal is the score: on each training
trace, your design's IPC divided by Mockingjay's IPC on the same trace, and the
geometric mean of those ratios over the training traces. Mockingjay, the seed and
the design to beat, scores exactly 1.0; larger is better, and above 1.0 beats it.
Two constraints hold for every design:
it must be realizable in hardware, and its replacement state may not exceed
@MAXKB@ KB, separate from the cache itself; a design declaring more is refused.

You write a design as a structure description, never as C++: a separate agent
writes the C++ from your description and from the meaning of every word in it,
so the description must say exactly what the policy does.

Return the whole candidate as ONE file inside a SINGLE fenced code block. Only
the first block in your reply is read; a section in its own block is lost and
the candidate is discarded unscored.

```
{MARKER.format(STRUCTURE_DESCRIPTION)}
<the structure description, YAML>
{MARKER.format(NEW_WORDS)}
<a YAML list of the new words the description uses, or nothing>
```

Both marker lines must appear, spelled exactly as above, even when there are no
new words.

Every candidate also reports its metadata, priced by CACTI at 22 nm from the
state the description declares (metadata_kb, metadata_area_mm2,
metadata_energy_uj); reported, not scored. The seed declares 47.4 KB of the
@MAXKB@ KB budget, so new state has to be paid for
by narrowing or removing state elsewhere; the metadata_kb figure in the feedback
tells you where a design stands.

THE VOCABULARY
The description is checked against a fixed vocabulary before anything is built.
Every key, symbol and value is defined in "The meaning of every word" at the end
of this message, which also says when ChampSim calls each event. An unknown key
or symbol, a value outside its declared width, or a reference to a name that is
not declared is refused, and the reason comes back to you.

NEW WORDS
Where the fixed vocabulary cannot express what you want, declare a new word in
the NEW_WORDS section and use it in the description. Each word is a mapping:
- name: lower-case letters, digits and underscores, starting with a letter
- place: where it may be written: condition (a clause of `when` or
  `bypass_when`), value (the value of a `set` or `install` field), step (a
  rule's action), or the name of a symbol list it joins: Extremum, TieBreak,
  SearchSideEffect, SignatureSource, FoldBit or SignatureHash
- params: the parameters it takes, each with a type: state, table, signature or
  sampler (the name of one declared in the description), access_types (a list
  of access types), {{int: [lo, hi]}} (an integer in that range), or
  {{symbol: <ListName>}}, where <ListName> is the name of ONE of the vocabulary's
  symbol lists, never a list of values: `cmp: {{symbol: Comparison}}` takes gt or
  any other member of Comparison. Inside a sampler, one of the sampled entry's
  fields, or its age, is passed as a `state` parameter. A symbol-list member
  takes no parameters.
- means: what the code does, in at least 8 words, exact enough that a programmer
  implements it without guessing: every input it reads, the arithmetic, the
  width, the order of operations, the tie-break.
`params` is a mapping from parameter name to type, never a list. A complete
declaration, one item of the NEW_WORDS list:
```
- name: reuse_below
  place: condition
  params: {{table: table, index: signature, limit: {{int: [0, 127]}}}}
  means: >
    True when the entry of the table at the index is present and its value is
    below the limit; an absent entry counts as below. Nothing is written.
```

Using a word in the description:
- a condition word is its own key in a `when` or `bypass_when` mapping, with its
  parameters as the value, and holds together with the other clauses there:
  `when: {{type_in: [LOAD], reuse_below: {{table: rdp, index: pc_sig, limit: 90}}}}`
- a step word is its own key in a rule, as the rule's one action:
  `- halve: {{field: etr}}`
- a value word is written where a value goes:
  `set: {{etr: {{clamped_sum: {{a: etr, b: etr, max: 11}}}}}}`
- a symbol-list member is written as that list's value: `tie_break: prefer_overdue`
A `when` or `bypass_when` clause is one of the grammar's condition forms or a
declared condition word, nothing else: a key of your own invention there (cmp,
op, threshold and the like) is refused. Declare a condition word instead. A
`when` is one mapping, never a list, and every clause in it must hold; each
kind of clause (counter, table, type_in, ...) appears in it at most once, so two
counter tests in one rule need a declared condition word that takes both.
`bypass_when` is a list, and the line is bypassed when any one item holds. Each
item is written as the inside of a `when` is, a mapping of its clauses, with no
`when:` key of its own:
`bypass_when: [{{table: {{table: rdp, index: pc_sig, cmp: gt, threshold: 73}}}}, {{reuse_below: {{table: rdp, index: pc_sig, limit: 90}}}}]`

Rules for new words:
- Additions only. A word of the fixed vocabulary is never changed; declare a
  new name instead.
- Every new word the description uses is declared in the same file, and every
  use passes exactly the declared parameters. A child keeps the declarations of
  the words it still uses and may drop the rest.
- A name accepted earlier in this run, by any candidate and not only by your
  parent, keeps its definition: never redeclare a name with a different one.
  Repeat an earlier declaration verbatim to reuse it; for a different meaning,
  choose a name no design of this run has used.
- A new word holds no storage of its own: every counter, table, field or sampler
  it reads or writes is declared in the description, so the metadata figure
  stays true.

WHAT COSTS YOU THE CANDIDATE (discarded with the reason, unscored)
- a missing marker line, or a description the vocabulary refuses
- a mapping with the same key twice: YAML would silently keep only the last, so
  the description is refused. A rule has one action; a second action is a
  second list item (`- train: ...` on its own line); a second field, table or
  signature is a second item of its list; and a `when` holds each kind of
  clause once (two `counter` tests need a declared condition word), never a
  second key in one mapping
- a `name` of more than 31 characters, or with anything but lower-case letters,
  digits and underscores: keep the seed's name or a short one, and put what
  changed in `doc`
- more than @MAXKB@ KB of declared replacement metadata at this LLC
- a new word with a thin meaning, a bad parameter type, or a clashing name
- C++ that cannot be made to compile from your description (rare; an exact
  description avoids it)
- a policy that bypasses a fill of type WRITE: ChampSim aborts that run, and a
  candidate is refused unless every training trace finishes
@SIZE@

WHAT COMES BACK
For every scored design: its score (IPC over Mockingjay's, trace by trace,
geometric mean: above 1.0 beats Mockingjay) and its metadata figures. When a
design is the parent of your next proposal you also see its feedback text, per
trace: IPC over Mockingjay's on that trace (vs MJ); the change from the closest
earlier design; LLC misses per 1000 instructions; the share of all cycles
stalled on the memory hierarchy, and of it the part stalled on LLC hits and the
part stalled on DRAM; the share of cycles spent retiring instructions, which no
replacement policy changes; the headroom, which is
the share of its sampled misses that Belady's MIN would have avoided on the same
stream; and how many of its sampled fills MIN judged as kept_dead (kept a line
never reused again while evicting one that was), evicted_sooner (evicted the
line reused soonest), should_bypass (inserted a line never reused before it was
evicted) and wrong_bypass (bypassed a line reused before every resident line),
each with the change from the closest earlier design in brackets. Each kind of
mistake is addressed by changing what the policy remembers or how it decides,
not by moving a threshold. For example, and not only these: wrong_bypass asks
for a bypass that decides from more evidence than one predicted value, such as
what the policy has learnt about the PC, the set or the access type;
should_bypass for telling, from what the policy remembers, which inserted lines
are never reused; kept_dead for a different notion of age; evicted_sooner for a
different victim order or promotion rule. The seed and the four other designs
you are shown have such parts: SRRIP's re-reference intervals, DRRIP's set dueling between
two insertion rules, SHiP's PC-signature table and its training, Mockingjay's
sampler and reuse-distance predictor. A trace with little headroom cannot be
helped much by replacement.

HOW TO PROPOSE
Every proposal changes the design: what a line, a set or a table remembers, how
the victim is chosen, how lines are inserted, promoted and aged, what the
signatures and tables observe and when they train, when and from what the policy
bypasses. The seed's constants are its authors' tuned values; moving one alone
is the least valuable step. A constant may move only together with the design
change it serves. You may redesign any part or all of the policy, or write a new design
from the vocabulary and your own words: a different kind of predictor, a
different signature, a second rule the sets choose between, a new state per line
or per set, a new event to train on, anything the vocabulary or a declared new
word can express. Build from the seed, the four other designs you are shown and
your own reasoning about the feedback; do not reproduce a policy from a paper or other
published work. The other designs at the end of this message show what
different structures look like in this vocabulary. Make each candidate a change
you can name, and start the description's `doc` with one
line saying what changed and why, so the feedback can be attributed; the `name`
field is not the place for it. Declare every field and table the policy keeps.
@SCOPE@
"""


def scope_text(max_change: int, max_lines: int) -> tuple[str, str]:
    form = ("Sizes are counted in canonical form: keys sorted, one key or list item per line, "
            f"comments and `doc` dropped; the seed is {seed_lines()} lines that way.")
    size = f"- a description of more than {max_lines} canonical lines, about three times the seed's"
    if max_change:
        size += f"\n- a change of more than {max_change} canonical lines against the closest earlier design"
        scope = (f"This run limits each step: a candidate may change at most {max_change} canonical lines\n"
                 "against its closest earlier design. A changed value counts 2, the old line and the new\n"
                 "(two constants: 4); a new word with its use about 10; a new field and the rule that uses\n"
                 "it 10 to 15; the victim rule replaced about 30. Make one change at a time; a larger\n"
                 f"change is refused unscored. {form}")
    else:
        scope = (f"Any part or all of the design may change in one step, within {max_lines} canonical\n"
                 f"lines in all. {form}")
    return size, scope


def max_kb() -> float:
    return float(os.environ.get("A3_MAX_KB") or 48)


OTHER_DESIGNS = (("lru", "LRU: recency stamps, the oldest evicted"),
                 ("srrip", "SRRIP: re-reference intervals, aged on a miss"),
                 ("drrip", "DRRIP: two insertion rules the sets choose between by set dueling"),
                 ("ship", "SHiP: a PC-signature table trained by a sampler, deciding insertion"))


def other_designs() -> str:
    parts = ["== Other designs written in this vocabulary ==",
             "Complete, validated designs, for the forms they use, not to copy. Any of their parts can be",
             "combined with any of the seed's, and a new word can express what none of them has."]
    for name, what in OTHER_DESIGNS:
        parts += [f"--- {what} ---", (POLICIES / f"{name}.yaml").read_text().rstrip()]
    return "\n".join(parts)


GUIDANCE_MAX = 8000
GUIDANCE_HEADING = "== Guidance from this run so far =="


def guidance_section(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if len(text) > GUIDANCE_MAX:
        text = text[:GUIDANCE_MAX] + "\n[cut at the cap]"
    return f"\n{GUIDANCE_HEADING}\n{text}\n"


def instructions(guidance: str = "") -> str:
    size, scope = scope_text(*limits())
    rules = RULES.replace("@SIZE@", size).replace("@SCOPE@", scope).replace("@MAXKB@", f"{max_kb():g}")
    assert "@SIZE@" not in rules and "@SCOPE@" not in rules and "@MAXKB@" not in rules
    return (f"{rules}\n{guidance_section(guidance)}{other_designs()}\n\n"
            f"== The meaning of every word ==\n{MEANINGS.read_text()}")


def packed(description: str, words: str = "") -> str:
    return (f"{MARKER.format(STRUCTURE_DESCRIPTION)}\n{description.rstrip()}\n"
            f"{MARKER.format(NEW_WORDS)}\n{words.strip()}\n")


def packed_seed(name: str = "mockingjay") -> str:
    return packed((POLICIES / f"{name}.yaml").read_text())
