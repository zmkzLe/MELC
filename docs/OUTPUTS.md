# What a run writes

Everything of a run is under `~/oe_out/<run>/`. The runs in `results/runs/` except `arm_archagent`
have the same layout; only the `.pid` files and simulation leftovers were removed. The files our code writes
are the same under either launcher. The search framework's own files differ (§4).

## 1. The records: `yaml_run/`

**`records.jsonl`** has one line per candidate, in the order evaluated; the first line is the seed
(Mockingjay). This is the main result file.

| Field | Meaning |
|---|---|
| `time`, `work` | when the evaluation started; its folder under `candidates/` |
| `status` | `ok` (scored) or why it was refused or failed (table below) |
| `error` | the reason, as sent back to the proposer |
| `id` | the design's identity: a hash of its canonical description and new words (name and `doc` excluded), so a repeat has the same id |
| `description`, `new_words_text`, `new_words` | the design as proposed, and its parsed new words |
| `score` | geometric mean over the training traces of IPC ÷ Mockingjay's IPC; the seed is 1.0 |
| `ipc`, `vs_mj` | per trace: IPC, and IPC ÷ Mockingjay's IPC |
| `closest` | the most similar earlier design (id and text similarity); the change is measured against it |
| `touched`, `change`, `change_lines` | the parts of the policy changed, the diff, and its size in canonical lines |
| `comparison` | score change and per-trace change against the closest earlier design |
| `declared_kb` | replacement state the description declares |
| `cost` | from CACTI at 22 nm: declared KB, bits per line, area (mm²) and energy (µJ) |
| `signals` | per trace: `profile` (LLC MPKI, cycles by cause, memory stalls by level, DRAM share) and `opt` (references, misses, Belady-MIN misses, headroom, each fill's verdict), plus `opt_vs_closest` |
| `cpp_model`, `cpp_calls`, `cpp` | the C++ writer's model, every call's tokens and seconds, the edit's size and whether the audit reverted anything |
| `seconds` | time in checks, C++ writing, simulation and signals |
| `run_seconds` | simulation time per trace |
| `reused_result` | set when the design was seen before: the earlier result stands, with no new C++ or simulation |
| `tokens`, `lengths`, `traces` | C++ writer tokens in all; warm-up and measured instructions; the traces |

`status` values:

| Status | Meaning |
|---|---|
| `ok` | scored |
| `malformed` | a section is missing, or the design section is empty |
| `too_large` | over 582 canonical lines |
| `words_refused` | a new word is badly declared, clashes with the vocabulary, or redefines a word accepted earlier in the run |
| `schema_rejected` | the description does not validate against the vocabulary plus its declared words |
| `step_too_large` | changes more lines than the run's step limit |
| `too_much_storage` | declares more than 48 KB of replacement state |
| `cpp_failed` | no C++ built after the edit, the repairs, and the fallback from the seed's C++ |
| `no_seed_result` | arrived before the seed was scored |

Refusals happen before any C++ is written, and cost nothing but the proposer's call.

**`store/<id>/`** holds each scored design once:
- `description.yaml`, `words.yaml`: the design and its new words;
- `policy.h`, `policy.cc`: the C++ that ran;
- `result.json`: score, IPCs and signals;
- `opt/`: the OPT report per trace.

**`candidates/<time>_<pid>/`** is each candidate's working folder, refused ones included:
- `candidate.txt`: the reply as received;
- `description.yaml`;
- `cpp/`: every C++ writer call, with its request, the model's reply and the compiler output;
- `stats/`: ChampSim's JSON per trace;
- `opt/`;
- `why.txt`: the feedback text sent to the proposer.

**`new_words_registry.jsonl`** holds every accepted new word with its full declaration: name,
place, typed parameters and meaning.

**`word_tallies.json`** counts, per word, the designs that used it, and how many of those beat,
matched or fell below their closest earlier design, or failed.

## 2. Held-out validation: `yaml_run/validation.jsonl` and `yaml_run/validation/`

One line per validator event:

| `event` | Meaning |
|---|---|
| `started` | a design began validation: its id, its training score, and how many validations are running |
| `validated` | finished: `score` has the geometric means over `all` 33 traces, the 17 `training` and the 16 `held_out`, against the seed's validation; `ipc` and `vs_mj` per trace; `gap` between training and held out; build and run times |
| `below_min_gain` | a new best on training, but less than 0.1% above the last one validated, so not validated |
| `superseded` | a queued best displaced by a newer best before it started |
| `deferred` | not started, because it could not finish by the deadline |

The seed is always validated first, and its IPCs are the reference for the rest. All runs are at
50M warm-up + 100M measured instructions. `validation/<id>/` holds `ipc.json` and the build
log. Nothing here is ever read by the search.

## 3. The drivers' logs

| File | What |
|---|---|
| `search.log` | the search's log; its first lines give the effective configuration (models, caps, temperature, islands, region, guidance) |
| `validator.log`, `adapt.log`, `copy.log` | the validator, the guidance writer (one line per version) and the bucket copies |
| `llm_calls.jsonl` | one line per proposer call: time, model, HTTP status (429 = rate-limited and retried), seconds, prompt, output and reasoning tokens, the temperature and seed as sent, the system prompt's size |
| `prompts.jsonl`, `prompts/` | OpenEvolve runs: every proposal's user message with its parent's score and status and the feedback it carried; each distinct system prompt once |
| `guidance.txt`, `guidance/`, `guidance.jsonl` | with guidance: the text the proposers read now; every version with the summary it was written from and the raw reply; one line per version with tokens and the paragraph |
| `seed_candidate.txt` | the seed as given to the search |

## 4. The search framework's own files

These come from OpenEvolve or SkyDiscover and are kept as they wrote them. Our records above are
the reference; these are the frameworks' views.

- `best/`: the framework's best program and its metrics. This is the training score at the
  search's length, not the held-out result.
- `checkpoints/`: database snapshots.
- `logs/`: the framework's own log.
- `adaevolve_iteration_stats_*.jsonl`: AdaEvolve's per-iteration island statistics.

## 5. Tables from the files

`python3 results/summarize.py [run folder ...]` recomputes every score from the per-trace IPCs
and prints:
- the runs;
- the refusals by reason;
- model calls and tokens.

`results/SUMMARY.md` is its output for `arm_adaevolve_noadapt_g3`.
