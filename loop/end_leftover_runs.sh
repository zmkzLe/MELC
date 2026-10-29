#!/bin/bash
# End the simulations a stopped search leaves behind.
#
#   end_leftover_runs.sh <search pid> <validator pid> <log file>
#
# Waits for the search to exit, then ends every running ChampSim copy (a /tmp/chia_run_*
# binary) that is not a descendant of the validator: the search's workers die with the
# search, but a simulation they started may not, and it would hold a core the validator needs.
set -u
SPID=$1; VPID=$2; LOG=$3
while kill -0 "$SPID" 2>/dev/null; do sleep 30; done
sleep 15
ps -eo pid,ppid,args | awk -v v="$VPID" '
  NR > 1 { pp[$1] = $2; cmd[$1] = $3 }
  END {
    for (p in cmd) if (cmd[p] ~ /^\/tmp\/chia_run_/) {
      a = p; mine = 0
      while ((a in pp) && a > 1) { if (a == v) { mine = 1; break }; a = pp[a] }
      if (!mine) print p
    }
  }' |
while read -r p; do
  kill -TERM "$p" 2>/dev/null && echo "$(date -u +%FT%TZ) end_leftover_runs: ended simulation pid $p left by the search" >> "$LOG"
done
