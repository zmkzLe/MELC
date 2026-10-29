# Running the loop

Two launchers start a run. They differ only in which program proposes the designs; the
evaluator, C++ writer, guidance writer, validator, leftover-run cleaner and bucket copy are shared.

| Launcher | Search | Driver |
|---|---|---|
| `launch_arm.sh` | OpenEvolve 0.3.2 (MAP-Elites with islands) | `run_openevolve.py` |
| `launch_native.sh <search>` | a SkyDiscover native search: `adaevolve`, `evox`, `gepa_native`, `openevolve_native`, `best_of_n`, `topk`, `beam_search` | `run_native.py` |

## 1. Launch

```
cd ~/chia_loop
./launch_arm.sh              <run> <max-change> <search stop> <deadline> gs://<bucket> [driver options]
./launch_native.sh <search>  <run> <max-change> <search stop> <deadline> gs://<bucket> [driver options]
```

- `<run>`: a new name; everything goes to `~/oe_out/<run>`. An existing name is refused.
- `<max-change>`: the most canonical lines a design may change against its closest earlier
  design. `600` switches the limit off in effect, since a whole design may have at most 582.
- `<search stop>`, `<deadline>`: UTC times written as `YYYY-MM-DDThh:mm:ssZ`. The search stops
  proposing at the first. The validator keeps going, two designs at a time from the stop on, never
  starts one it cannot finish by the deadline, and is killed ten minutes after it.
- `gs://<bucket>`: the run is copied there every hour and once more at the end.
- Driver options go last. The ones the runs used:
  - `--ensemble MODEL=WEIGHT,...`: the proposers, one drawn per proposal;
  - `--max-tokens N`, `--llm-timeout S`: the proposer's output cap and timeout;
  - `--traces a,b --warmup N --sim N --iterations N`: other traces, run lengths or a cap on proposals.

  The launchers already pass temperature 0.7 with no model seed, 2 retries, 2 candidates at a
  time, 2 islands with migration every 50, and a 3-hour cap per candidate.

A launch starts, detached, each with a `.pid` file and a log in the run folder:

1. the search;
2. the held-out validator;
3. with `ADAPT_EVERY` set, the guidance writer;
4. the leftover-run cleaner (`end_leftover_runs.sh`), which ends simulations the search leaves behind when it stops;
5. the hourly copy to the bucket.

Stop a run with `./stop_arm.sh <run>` once its validator is done (`--kill-validation` ends a
validation in flight). At the stop time the search stops proposing but does not always exit,
and the script ends every process of the run cleanly and makes the final copy.

## 2. Settings in the environment

| Variable | Default | What it sets |
|---|---|---|
| `ADAPT_EVERY` | unset (no guidance) | rewrite the guidance every K candidates; the runs used 10 |
| `A3_VERTEX_REGION` | `us-central1` | Vertex location for every model call; the Gemini 3.x models need `global` |
| `A3_CPP_MODEL` | `gemini-2.5-pro` | the model that writes, repairs and audits the C++ |
| `A3_GUIDANCE_MODEL` | `gemini-2.5-pro` | the model that writes the guidance paragraph |
| `A3_CPP_MAX_TOKENS`, `A3_CPP_TIMEOUT` | `32000`, `300` | the C++ writer's output cap (reasoning included) and timeout per call |
| `A3_REPAIRS` | `2` | repair calls when an edit does not compile, per attempt |
| `A3_AUDIT` | `1` | `0` turns off the audit call that reverts unrequested C++ changes |
| `A3_MAX_KB` | `48` | the most replacement state a design may declare, in KB |
| `A3_MAX_LINES` | three times the seed (582) | the most canonical lines a design may have |
| `CHIA_WORKERS` | cores − 2 | simulations at once per candidate |
| `EVALUATOR` | `yaml` | `launch_arm.sh` only: `ae` runs the comparison arm where the model writes the C++ itself |
| `VALIDATOR_EXTRA` | empty | extra options for the validator |

The drivers set `A3_RUN_DIR`, `A3_TRACES`, `A3_WARMUP`, `A3_SIM`, `A3_MAX_CHANGE` and `A3_RESULT`
for the evaluator's workers themselves.

## 3. The runs in `results/`

| Run | Launch | Proposers (`--ensemble`, 0.7 / 0.3) | Proposer cap | C++ and guidance model | Region | `ADAPT_EVERY` |
|---|---|---|---|---|---|---|
| `arm_adaevolve_noadapt_g3` | `launch_native.sh adaevolve` | gemini-3.1-pro-preview / gemini-3.8-flash | 64,000, 900 s | gemini-3.1-pro-preview | global | — |

It used `<max-change>` 600. These settings are read from each run's `search.log`,
`adapt.log` and call logs; "—" means the copy of the run does not record it. The C++ writer's cap
is not recorded in the run files; its longest reply, 63,996 tokens, shows it was 64,000.

For example, run AdaEvolve on the 3.x models as `arm_adaevolve_noadapt_g3` ran, with the code in `loop/`:

```
A3_VERTEX_REGION=global A3_CPP_MODEL=gemini-3.1-pro-preview A3_CPP_MAX_TOKENS=64000 A3_CPP_TIMEOUT=900 \
  ./launch_native.sh adaevolve my_ada 600 <stop> <deadline> gs://<bucket> \
  --ensemble gemini-3.1-pro-preview=0.7,gemini-3.8-flash=0.3 --max-tokens 64000 --llm-timeout 900
```
