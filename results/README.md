# Results

`runs/` holds seven runs, one Google Cloud VM each. Every file each run wrote is included except the
`.pid` files and simulation leftovers; `docs/OUTPUTS.md` says what each file is. `summarize.py`
recomputes every table from these files, and `SUMMARY.md` is its output.

All runs share the machine (DPC4 single core, 3 MB LLC, no prefetchers), the traces (17 training
at 20M + 50M, all 33 for validation at 50M + 100M), the seed (Mockingjay) and the evaluator, except
`arm_archagent`, whose model writes the C++ directly. They
differ in the search, the models, the settings and their length, so no two rows are a controlled
comparison.

| Folder | Search | Models (proposers) |
|---|---|---|
| `arm_adaevolve_noadapt_g3` | AdaEvolve | Gemini 3.1 Pro / 3.8 Flash |

The exact commit each SkyDiscover run used is not recorded in its files; `loop/` is the latest
version of that code.

**How to read the numbers.** Every score is IPC relative to Mockingjay on the same trace, as a
geometric mean over traces. The prompt of `arm_archagent` said its score was relative to LRU, an
error in the prompt: like every run, it was scored relative to Mockingjay. Each run is one sample of a noisy search, and the gains found are under
1%. For evolved designs, the C++ was only required to build and finish every trace. The storage
and area figures are computed from the description.
