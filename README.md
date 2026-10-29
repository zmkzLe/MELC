# Evolving cache replacement policies as structured descriptions

A loop that evolves last-level-cache (LLC) replacement policies with LLMs. The proposing model
never edits simulator code. Instead, it writes each design as a **structured YAML description** in
a fixed vocabulary, and it may add new words to that vocabulary if it defines each one precisely.
A separate LLM call then translates each valid description into ChampSim C++. Each candidate gets
back an explanation of *why* it scored as it did, not only a score. Optionally, the search rewrites part of its own prompt every
10 candidates. Every new best is re-checked automatically on traces the search never sees.

The same evaluator runs under several search algorithms: OpenEvolve, and SkyDiscover's native
AdaEvolve, EvoX, GEPA, OpenEvolve, Best-of-N, Top-K and beam search.

Submitted to the 1st A3 Workshop CHIA Hackathon.

## How it works

| Step | What it does | Code |
|---|---|---|
| **Propose** | A Gemini ensemble (Pro 70% / Flash 30%, temperature 0.7, no seed) writes one design as YAML plus any new words, from a parent design, its feedback and, if enabled, the current guidance. Besides the seed (Mockingjay), the prompt shows LRU, SRRIP, DRRIP and SHiP written in the same vocabulary. | `loop/run_openevolve.py` (OpenEvolve), `loop/run_native.py` (SkyDiscover), `loop/evolver_prompt.py` |
| **Check** | Before any cost: both sections present; at most 582 canonical lines; new words well formed; the description validates against the vocabulary plus its declared words; at most 48 KB of declared replacement state; no redefinition of a word accepted earlier in the run. A refusal takes under a second and its reason goes back to the model. | `champsim/structure_description/`, `loop/design_evaluator.py` |
| **Translate** | A separate model edits the C++ of the closest earlier design for the change; up to 2 repairs if it does not compile, then the same from the seed's C++. An audit call reverts any change the description did not ask for. | `loop/cpp_writer.py`, `loop/seed_cpp/` |
| **Evaluate** | ChampSim, DPC4 single-core configuration (3 MB LLC, 4096 sets × 12 ways, no prefetchers), on 17 training traces at 20M warm-up + 50M instructions, in parallel. The score is the geometric mean over traces of IPC divided by Mockingjay's IPC; the seed scores 1.0. | `champsim/`, `loop/chia_llc_loop.py` |
| **Explain** | Per trace: IPC against Mockingjay, LLC MPKI, the stall profile, the policy's mistakes against Belady's optimal policy on 256 sampled sets, and the storage's area and energy from CACTI. It all goes back to the model as text. | `champsim/feedback/`, `loop/design_evaluator.py` |
| **Adapt** (optional) | Every K candidates, a writer summarises what paid off and what was refused, lists the accepted words, and one capped model call writes a guidance paragraph into every later prompt. | `loop/guidance_writer.py` |
| **Validate** | Every new best at least 0.1% above the last validated one runs on all 33 traces at 50M + 100M instructions, in its own ChampSim tree; 16 of the traces are never seen by the search. Nothing it measures goes back to the search. | `loop/heldout_validator.py` |
| **Operate** | One command starts the search, the validator, the guidance writer, a cleaner for the simulations a stopped search leaves behind, and hourly copies to a bucket; one command stops everything cleanly. | `loop/launch_arm.sh`, `loop/launch_native.sh`, `loop/stop_arm.sh`, `loop/end_leftover_runs.sh` |

## Layout

```
loop/       the loop: drivers, evaluator, prompt, C++ writer, guidance writer, validator, launch scripts
champsim/   the DPC4 ChampSim fork: vocabulary and validator (structure_description/), feedback
            (feedback/), the build/run node (chia_node/), the Mockingjay port, CACTI (tools/cacti),
            the trace list (selected_traces.txt)
setup/      trace_seconds_50M100M.json (wall time per trace, for the validator)
results/    runs/ (seven runs, every file they wrote),
            summarize.py (rebuilds every table from runs/), SUMMARY.md (its output), README.md
docs/       SETUP.md, RUNNING.md, OUTPUTS.md
```

## Quick start

1. **Set up a machine** as in [`docs/SETUP.md`](docs/SETUP.md): a Linux VM with Gemini through
   Vertex AI, ChampSim built twice, the 33 traces, and a Cloud Storage bucket.
2. **Launch a run** as in [`docs/RUNNING.md`](docs/RUNNING.md), with the paper's models. One of the
   paper's searches, for example AdaEvolve (the others: `gepa_native`, `evox`, `openevolve_native`,
   `best_of_n`, `topk`):
   ```
   cd ~/chia_loop
   A3_VERTEX_REGION=global A3_CPP_MODEL=gemini-3.1-pro-preview A3_CPP_MAX_TOKENS=64000 A3_CPP_TIMEOUT=900 \
   ./launch_native.sh adaevolve my_run 600 <search stop> <deadline> gs://my-bucket \
       --ensemble gemini-3.1-pro-preview=0.7,gemini-3.8-flash=0.3 --max-tokens 64000 --llm-timeout 900
   ```
   The ArchAgent-style baseline, where the model writes the C++ directly, starts the same way with
   `EVALUATOR=ae ./launch_arm.sh` in place of `./launch_native.sh adaevolve`.
3. **Stop it** with `./stop_arm.sh my_run`.
4. **Read what it wrote** with [`docs/OUTPUTS.md`](docs/OUTPUTS.md), and tabulate it with
   `python3 results/summarize.py ~/oe_out/my_run`.

## Credits and license

ChampSim is used under its own license (`champsim/LICENSE`) and is modified here. The Mockingjay
policy is ported from its authors' code. The searches use OpenEvolve 0.3.2 and SkyDiscover. The
rest of this repository does not carry a license yet.
