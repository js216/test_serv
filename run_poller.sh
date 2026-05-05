#!/bin/bash
# run_poller.sh -- supervised poller that respawns on crash.
#
# A C-extension heap corruption (pyft4222, ftd2xx, pyusb, etc.)
# can SIGABRT the poller. Without supervision, the bench goes
# offline until an operator notices and manually restarts. With
# this wrapper, the crash becomes a transient blip: a few seconds
# offline, then the next session lands normally.
#
# Use this in production. For a debug session where you want the
# crash to be visible immediately, run `python3 poller.py` directly.
#
# Optional: set TEST_SERV_MALLOC_CHECK=1 to enable glibc's malloc
# tracing -- the next crash will print a stack hint to stderr
# (slower; enable only when actively hunting a heap bug).

set -u

cd "$(dirname "$0")"

if [[ "${TEST_SERV_MALLOC_CHECK:-0}" = "1" ]]; then
  export MALLOC_CHECK_=3
  export PYTHONFAULTHANDLER=1
  echo "$(date) supervisor: MALLOC_CHECK_=3 PYTHONFAULTHANDLER=1 enabled"
fi

# Cooldown between respawns: too short and a crashing-on-startup
# loop spins the CPU; too long and the bench is unresponsive after
# a transient fault. 5s is the right balance for a bench tool.
COOLDOWN=5
RUN_BUDGET_S=10   # if the child dies inside this window, count it
MAX_FAST_FAILS=5  # > this many fast fails in a row -> stop respawning

fast_fails=0
while true; do
  start=$(date +%s)
  python3 poller.py "$@"
  rc=$?
  end=$(date +%s)
  elapsed=$((end - start))

  if [[ $rc -eq 0 ]]; then
    echo "$(date) supervisor: poller exited cleanly (rc=0); not respawning"
    exit 0
  fi
  if [[ $rc -eq 130 ]]; then
    echo "$(date) supervisor: poller exited via SIGINT (rc=130); not respawning"
    exit 0
  fi

  if [[ $elapsed -lt $RUN_BUDGET_S ]]; then
    fast_fails=$((fast_fails + 1))
    echo "$(date) supervisor: poller died after ${elapsed}s (rc=$rc, fast-fail $fast_fails/$MAX_FAST_FAILS)"
    if [[ $fast_fails -ge $MAX_FAST_FAILS ]]; then
      echo "$(date) supervisor: too many fast fails; giving up so the operator can investigate"
      exit 1
    fi
  else
    fast_fails=0
    echo "$(date) supervisor: poller died after ${elapsed}s (rc=$rc); respawning in ${COOLDOWN}s"
  fi
  sleep $COOLDOWN
done
