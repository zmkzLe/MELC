# Setting up a machine

The runs in `results/` each had one Google Cloud VM: `n2-standard-48` (48 vCPUs, 192 GB),
Ubuntu 22.04, a 100 GB disk, Gemini through Vertex AI. With two candidates at a time, the search
runs up to 34 simulations and the validator 8, so 48 cores leave room. A smaller machine works;
set `CHIA_WORKERS` (simulations per candidate, default cores − 2) and expect longer runs.

## 1. Where things go

The launch scripts expect this layout in the home directory:

| Path | What |
|---|---|
| `~/champsim` | `champsim/` from this repository, built (§3). The search builds every candidate here. |
| `~/champsim_validate` | a second built copy, used only by the held-out validator |
| `~/chia_loop` | `loop/` from this repository |
| `~/traces` | the traces, as `<name>.champsimtrace.xz` (§4) |
| `~/loop_out/trace_seconds_50M100M.json` | `setup/trace_seconds_50M100M.json`: each trace's wall time at 50M + 100M, measured from LRU runs on our cluster, so validation starts the longest first |
| `~/miniconda3/envs/chia_env` | the Python environment (§2); the scripts call `~/miniconda3/envs/chia_env/bin/python` |
| `~/oe_out/<run>` | created by each launch: every file of the run (`docs/OUTPUTS.md`) |

## 2. Python

A conda environment named `chia_env` with Python 3.10:

- `pip install google-auth requests` and `pip install -r champsim/requirements.txt` (pydantic, PyYAML).
- **OpenEvolve and SkyDiscover.** SkyDiscover is the `skydiscover` submodule of
  [ucb-bar/evolve-flows](https://github.com/ucb-bar/evolve-flows); the runs' machine image had it
  from the `public-release-v1` branch at commit `78833ea`, with OpenEvolve 0.3.2 (commit
  `411fb59`). Two things to know when installing it:
  - the submodule's URL is an SSH URL; without a GitHub SSH key, change it to HTTPS in
    `.gitmodules` before `git submodule update --init --recursive`;
  - SkyDiscover pins Google's `alpha-evolve` client through uv sources, which pip ignores, so
    install that first (`pip install git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud`),
    then `pip install -e skydiscover`.
- **CHIA (optional).** The loop calls the ChampSim node's build and run functions directly and
  does not need CHIA. With [CHIA](https://github.com/ucb-bar/chia) 1.0.1 (commit `a2c4dae`, Ray
  2.54.0) installed, the same code is also available as a CHIA node,
  `champsim/chia_node/champsim.py: DPC4ChampSimNode`.

## 3. ChampSim, twice

```
cp -a <this repository>/champsim ~/champsim && cd ~/champsim
git clone https://github.com/microsoft/vcpkg.git vcpkg
git -C vcpkg checkout 1de2026f28ead93ff1773e6e680387643e914ea1
vcpkg/bootstrap-vcpkg.sh && vcpkg/vcpkg install
(cd tools/cacti && make)                    # CACTI, which prices the replacement state
cp -a ~/champsim ~/champsim_validate        # the validator's own tree
cp -a <this repository>/loop ~/chia_loop
mkdir -p ~/loop_out && cp <this repository>/setup/trace_seconds_50M100M.json ~/loop_out/
```

- The machine is DPC4's single-core full-bandwidth configuration without prefetchers,
  `champsim/dpc4/1C.fullBW.nopref.mockingjay.json`: a 3 MB LLC of 4096 sets × 12 ways, DRAM at
  4800 MT/s, no prefetcher at any level. The candidate's module replaces the LLC's policy. Each candidate is built into
  `replacement/evolved_policy/` by the node, which runs `config.sh` and `make` itself.
- Keep the set of folders under `replacement/` fixed during a run: ChampSim compiles every module
  folder, so a broken leftover breaks every build, and adding or removing a folder makes the next
  build a clean one (about 2 minutes instead of 7 seconds).
- CACTI must stay at `champsim/tools/cacti/cacti`; the feedback runs it from that folder.

## 4. Traces

`champsim/selected_traces.txt` lists the 33 traces, marked `training` (17, used by the search)
or `held-out` (16, used only by the validator): per SPEC CPU2006 and CPU2017 benchmark, the trace
with the highest LLC MPKI among those above 1 under LRU. Download each from the DPC-3 trace
release into `~/traces`:

```
https://dpc3.compas.cs.stonybrook.edu/champsim-traces/speccpu/<name>.champsimtrace.xz
```

About 15 GB in all.

## 5. Google Cloud

- **Vertex AI** enabled in the VM's project, and the VM's service account with the
  `cloud-platform` scope; the drivers get a token from it and refresh it. The Gemini 3.x models
  answer only from Vertex's `global` location: set `A3_VERTEX_REGION=global` for them.
- **A Cloud Storage bucket** the VM can write. The launch scripts copy the run there every hour
  and refuse to start without one.

## 6. Checks before a real run

With `cd ~/chia_loop && PY=~/miniconda3/envs/chia_env/bin/python`:

- **The native searches:** `$PY check_native.py --search <type>`. It calls no model and no
  simulator; it checks that the search's settings are applied and that scores and feedback reach it.
- **One candidate at toy length.** The seed, on two traces at 1M + 5M, with no model call; in a
  fresh run folder it must score exactly 1.0:
  ```
  $PY -c "import evolver_prompt as y, pathlib; pathlib.Path.home().joinpath('seed.txt').write_text(y.packed_seed())"
  CHAMPSIM_ROOT=$HOME/champsim PYTHONPATH=$HOME/champsim A3_RUN_DIR=$HOME/oe_out/smoke \
    A3_TRACES=429.mcf-192B,483.xalancbmk-127B A3_WARMUP=1000000 A3_SIM=5000000 \
    $PY -u design_evaluator.py ~/seed.txt
  ```
