#!/bin/bash
# One run, searched by one of skydiscover's own algorithms.
# launch_arm.sh's twin: the same validator, leftover-run cleaner and hourly copy, with
# run_native.py in place of run_openevolve.py, so an arm differs from the
# OpenEvolve arm in the algorithm proposing designs and nothing else.
#
#   launch_native.sh <search> <run-name> <max-change> <search-stop> <deadline> <bucket> [driver args...]
#   launch_native.sh adaevolve arm_adaevolve 600 <YYYY-MM-DDThh:mm:ssZ> <YYYY-MM-DDThh:mm:ssZ> gs://<bucket>
#
# <search>: adaevolve, gepa_native, openevolve_native, evox, topk, best_of_n, beam_search.
# Times are ISO 8601 in UTC (a trailing Z). The search stops at <search-stop>;
# the validator runs two at once from then on, defers what cannot end by
# <deadline>, and is killed ten minutes after it. Every process writes under
# ~/oe_out/<run-name>: search.log, validator.log, copy.log and a .pid each.
# Extra driver arguments follow the bucket; VALIDATOR_EXTRA in the environment
# is appended to the validator's command (a dry run at short length:
#   VALIDATOR_EXTRA='--traces a,b --warmup 1000000 --sim 5000000').
# ADAPT_EVERY=K makes this the adaptive arm, exactly as launch_arm.sh does: a
# fifth process (guidance_writer.py, model A3_GUIDANCE_MODEL) rewrites
# <out>/guidance.txt every K candidates and the driver reads it at every
# proposal (--guidance). stop_arm.sh <run-name> stops everything cleanly.
set -euo pipefail
[ $# -ge 6 ] || { sed -n '2,21p' "$0"; exit 2; }
SEARCH=$1; RUN=$2; MAXCHANGE=$3; STOP=$4; DEADLINE=$5; BUCKET=$6; shift 6
case "$SEARCH" in adaevolve|gepa_native|openevolve_native|evox|topk|beam_search|best_of_n) ;;
  *) echo "unknown search $SEARCH"; exit 1 ;; esac

export CHAMPSIM_ROOT=$HOME/champsim PYTHONPATH=$HOME/champsim
# Every Vertex call of the run -- proposers, C++ writer -- goes to
# A3_VERTEX_REGION (read when run_openevolve is imported):
# us-central1 answers for the 2.5 family, the 3.x models only from global.
# A3_CPP_MODEL writes the C++. Both exported here because the run is detached.
export A3_CPP_MODEL=${A3_CPP_MODEL:-gemini-2.5-pro}
export A3_VERTEX_REGION=${A3_VERTEX_REGION:-us-central1}
PY=$HOME/miniconda3/envs/chia_env/bin/python
OUT=$HOME/oe_out/$RUN
LOOP=$(cd "$(dirname "$0")" && pwd)   # the loop this script lives in

now=$(date +%s); stop_s=$(date -d "$STOP" +%s); dead_s=$(date -d "$DEADLINE" +%s)
search_timeout=$((stop_s - now)); validator_timeout=$((dead_s - now + 600))
[ "$search_timeout" -gt 0 ] || { echo "the search's stop time $STOP is in the past"; exit 1; }
[ "$dead_s" -gt "$stop_s" ] || { echo "the deadline must be after the search's stop time"; exit 1; }
[ -d "$HOME/champsim_validate/replacement" ] || { echo "no validator tree at ~/champsim_validate"; exit 1; }
for f in trace_seconds_50M100M.json; do
  [ -s "$HOME/loop_out/$f" ] || { echo "missing ~/loop_out/$f"; exit 1; }
done
[ ! -e "$OUT" ] || { echo "$OUT exists; pick another run name or remove it"; exit 1; }
gsutil ls "$BUCKET" > /dev/null || { echo "cannot list $BUCKET; is it there, and can this VM write to it?"; exit 1; }
ADAPT=()
if [ -n "${ADAPT_EVERY:-}" ]; then
  [ "$ADAPT_EVERY" -gt 0 ] 2>/dev/null || { echo "ADAPT_EVERY must be a positive count of candidates"; exit 1; }
  ADAPT=(--guidance "$OUT/guidance.txt")
fi
mkdir -p "$OUT/yaml_run"
cd "$LOOP"

nohup timeout "$search_timeout" "$PY" -u run_native.py --search "$SEARCH" --vertex --temperature 0.7 \
    --max-change "$MAXCHANGE" --iterations 100000 --parallel 2 --llm-retries 2 --eval-timeout 10800 \
    --islands 2 --migration-interval 50 \
    --out "$OUT" "${ADAPT[@]}" "$@" > "$OUT/search.log" 2>&1 < /dev/null &
echo $! > "$OUT/search.pid"

if [ -n "${ADAPT_EVERY:-}" ]; then
  nohup timeout "$search_timeout" "$PY" -u guidance_writer.py --run-dir "$OUT/yaml_run" --out "$OUT/guidance.txt" \
      --every "$ADAPT_EVERY" --model "${A3_GUIDANCE_MODEL:-gemini-2.5-pro}" \
      --search-pid "$(cat "$OUT/search.pid")" > "$OUT/adapt.log" 2>&1 < /dev/null &
  echo $! > "$OUT/adapt.pid"
fi

nohup timeout "$validator_timeout" "$PY" -u heldout_validator.py --run-dir "$OUT/yaml_run" \
    --tree "$HOME/champsim_validate" \
    --durations "$HOME/loop_out/trace_seconds_50M100M.json" --search-stop "$STOP" --deadline "$DEADLINE" \
    ${VALIDATOR_EXTRA:-} > "$OUT/validator.log" 2>&1 < /dev/null &
echo $! > "$OUT/validator.pid"

VPID=$(cat "$OUT/validator.pid")
# After the search exits, end any simulation it left behind (end_leftover_runs.sh).
nohup "$LOOP/end_leftover_runs.sh" "$(cat "$OUT/search.pid")" "$VPID" "$OUT/search.log" > /dev/null 2>&1 < /dev/null &
echo $! > "$OUT/cleaner.pid"

# Hourly while the validator lives (it outlives the search), then one last copy.
nohup bash -c "copy() { gsutil -m -q rsync -r -x '.*\.opt\.log$' '$OUT' '$BUCKET/$RUN' >> '$OUT/copy.log' 2>&1 \
      && echo \"copied \$(date -u +%FT%TZ)\" >> '$OUT/copy.log' \
      || echo \"COPY FAILED \$(date -u +%FT%TZ)\" >> '$OUT/copy.log'; }
    while kill -0 $VPID 2>/dev/null; do copy; for i in \$(seq 60); do kill -0 $VPID 2>/dev/null || break; sleep 60; done; done
    copy; echo \"final copy, validator gone\" >> '$OUT/copy.log'" > /dev/null 2>&1 < /dev/null &
echo $! > "$OUT/copy.pid"

echo "started $RUN ($SEARCH): search stops in $((search_timeout / 3600)) h, validator killed in $((validator_timeout / 3600)) h"
echo "  search    pid $(cat "$OUT/search.pid")   -> $OUT/search.log"
echo "  validator pid $(cat "$OUT/validator.pid")   -> $OUT/validator.log"
echo "  copy      pid $(cat "$OUT/copy.pid")   -> $BUCKET/$RUN every hour"
echo "  cleaner   pid $(cat "$OUT/cleaner.pid")   -> ends simulations the stopped search leaves behind"
echo "  C++       $A3_CPP_MODEL; vertex region $A3_VERTEX_REGION"
[ -z "${ADAPT_EVERY:-}" ] || echo "  adapt     pid $(cat "$OUT/adapt.pid")   -> $OUT/guidance.txt every $ADAPT_EVERY candidates (${A3_GUIDANCE_MODEL:-gemini-2.5-pro})"
