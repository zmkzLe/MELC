#!/bin/bash
# Stop one arm cleanly, in the order the processes depend on each other.
#
#   stop_arm.sh <run-name> [--kill-validation]
#
# The search gets SIGTERM (timeout forwards it; OpenEvolve shuts down gracefully and
# saves best/), the leftover-run cleaner then ends the simulations the search left behind,
# the guidance writer exits, the validator and the simulations under it are ended,
# and the copy loop makes its final copy and exits. A validation in flight would be
# lost, so the script refuses while one runs unless --kill-validation is given.
# Afterwards nothing of the run may remain; leftover /tmp/chia_run_* directories are
# removed once no simulation runs on the machine.
set -u
RUN=${1:?run name}; KILLV=${2:-}
O=$HOME/oe_out/$RUN
[ -d "$O" ] || { echo "no run at $O"; exit 1; }

pid() { cat "$O/$1.pid" 2>/dev/null; }
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
descendants() { local k; for k in $(pgrep -P "$1"); do echo "$k"; descendants "$k"; done; }
wait_gone() { local n=0; while alive "$1" && [ "$n" -lt "$2" ]; do sleep 1; n=$((n + 1)); done; ! alive "$1"; }
end_all() { local p; for p in "$@"; do kill -TERM "$p" 2>/dev/null; done; sleep 3
            for p in "$@"; do kill -KILL "$p" 2>/dev/null; done; }

V=$O/yaml_run/validation.jsonl
[ -f "$V" ] || V=$O/ae_run/validation.jsonl
if [ -f "$V" ]; then
  running=$(python3 - "$V" <<'EOF'
import json, sys
ev = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
done = {e["id"] for e in ev if e["event"] in ("validated", "failed")}
print(" ".join(e["id"] for e in ev if e["event"] == "started" and e["id"] not in done))
EOF
)
  if [ -n "$running" ] && alive "$(pid validator)" && [ "$KILLV" != "--kill-validation" ]; then
    echo "a validation is running ($running) and would be lost: wait for it, or pass --kill-validation"; exit 1
  fi
fi

S=$(pid search); G=$(pid cleaner); A=$(pid adapt); VP=$(pid validator); C=$(pid copy)
skids=$(alive "$S" && descendants "$S")
if alive "$S"; then
  echo "$(date -u +%FT%TZ) search pid $S: SIGTERM, waiting for OpenEvolve's shutdown"
  kill -TERM "$S"
  # OpenEvolve saves best/ at the signal, then waits for the evaluations in flight, which at
  # full length is up to an hour; those candidates are lost either way, so the wait is short.
  wait_gone "$S" 180 || { echo "the search did not exit in 3 min: killing its processes"; end_all $skids "$S"; }
else
  echo "the search is not running"
fi
if alive "$G"; then
  echo "$(date -u +%FT%TZ) waiting for the leftover-run cleaner (it ends the search's leftover simulations 15 s after the search exits)"
  wait_gone "$G" 180 || echo "the cleaner did not exit in 3 min"
fi
leftover=""; for p in $skids; do alive "$p" && leftover="$leftover $p"; done
[ -z "$leftover" ] || { echo "ending processes the search left:$leftover"; end_all $leftover; }
if alive "$A"; then kill -TERM "$A"; wait_gone "$A" 30 || kill -KILL "$A" 2>/dev/null; echo "guidance writer ended"; fi

if alive "$VP"; then
  vkids=$(descendants "$VP")
  echo "$(date -u +%FT%TZ) validator pid $VP: ending it and $(echo $vkids | wc -w) processes under it"
  kill -TERM "$VP"; wait_gone "$VP" 60 || kill -KILL "$VP" 2>/dev/null
  [ -z "$vkids" ] || end_all $vkids
else
  echo "the validator is not running"
fi
if alive "$C"; then
  echo "$(date -u +%FT%TZ) waiting for the copy loop's final copy"
  wait_gone "$C" 900 || echo "the copy loop did not exit in 15 min"
fi

left=""
for p in $S $G $A $VP $C $skids ${vkids:-}; do alive "$p" && left="$left $p"; done
if [ -n "$left" ]; then echo "STILL RUNNING, this run's:"; ps -o pid,etime,args -p "$(echo $left | tr ' ' ,)"; exit 1; fi
others=$(pgrep -f "^/tmp/chia_run_" || true)
if [ -n "$others" ]; then
  echo "simulations of another run are still on this machine (left alone): $(echo $others | wc -w)"
else
  rm -rf /tmp/chia_run_*
fi
echo "$(date -u +%FT%TZ) stopped $RUN; copy log: $(tail -1 "$O/copy.log" 2>/dev/null)"
