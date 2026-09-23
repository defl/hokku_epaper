#!/usr/bin/env bash
# Run the big capture to completion without supervision.
#
# judge_capture verifies a stored frame by re-rendering it rather than trusting
# the filename, so restarting is safe and skips what is already done. The failures
# this guards against are transient: a panel console that does not answer, an ssh
# hiccup to the camera Pi, a refresh that times out. Any of them kills one run of
# an eight-hour job, and there is nobody watching.
#
# It stops for the two things a retry cannot fix: the disk filling, and a run that
# makes no progress at all.

set -u
PLAN=build/camcal/bigrun_plan.json
OUT=build/camcal/session6
LOG=build/camcal/bigrun_capture.log
PY=.venv/Scripts/python.exe
TOTAL=$(grep -c '"tag"' "$PLAN")
MIN_FREE_GB=12

shots() { ls "$OUT"/*__shot.jpg 2>/dev/null | wc -l; }
free_gb() { df -BG . | awk 'NR==2 {gsub("G","",$4); print $4}'; }

mkdir -p "$OUT"
echo "supervisor: $TOTAL captures planned, $(shots) already present" | tee -a "$LOG"

for attempt in $(seq 1 40); do
  done_before=$(shots)
  if [ "$done_before" -ge "$TOTAL" ]; then
    echo "supervisor: all $TOTAL captures present" | tee -a "$LOG"
    exit 0
  fi
  if [ "$(free_gb)" -lt "$MIN_FREE_GB" ]; then
    echo "supervisor: STOPPING, only $(free_gb)G free" | tee -a "$LOG"
    exit 2
  fi

  echo "supervisor: attempt $attempt, $done_before/$TOTAL done, $(free_gb)G free" | tee -a "$LOG"
  "$PY" -u tools/judge_capture.py --plan "$PLAN" --port COM4 --bench-flip180 \
      --outdir "$OUT" >> "$LOG" 2>&1
  rc=$?
  done_after=$(shots)
  echo "supervisor: attempt $attempt exited $rc, $done_after/$TOTAL done" | tee -a "$LOG"

  [ "$done_after" -ge "$TOTAL" ] && { echo "supervisor: complete" | tee -a "$LOG"; exit 0; }
  # A retry that captured nothing means the fault is not transient.
  if [ "$done_after" -le "$done_before" ]; then
    stalls=$((${stalls:-0} + 1))
    if [ "$stalls" -ge 3 ]; then
      echo "supervisor: STOPPING, three attempts with no progress" | tee -a "$LOG"
      exit 3
    fi
  else
    stalls=0
  fi
  sleep 30
done
echo "supervisor: STOPPING, attempt limit reached at $(shots)/$TOTAL" | tee -a "$LOG"
exit 4
