#!/usr/bin/env bash
# Wait for the LUT family, then plan, capture and build the judging page.
#
# One chain rather than four hand-run steps: the builds take about 70 minutes and
# the capture about two hours, and nothing in between needs a decision. Each step
# refuses to start on a failed predecessor, so a broken LUT does not quietly
# become a page full of identical renders.

set -eu
PY=.venv/Scripts/python.exe
LUTS=build/camcal/luts
PLAN=build/camcal/lut_plan.json
OUT=build/camcal/session7
LOG=build/camcal/lut_round.log
EXPECTED="b50_t85 b00_t85 b25_t85 b100_t85 b50_t70 b50_t100"

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# Liveness by progress, not by process lookup: the builder is a Windows
# python.exe and pgrep from MSYS cannot see it, so an earlier version of this
# declared it dead while it was running fine. A build takes ~12 minutes, so
# nothing changing for 30 means something is actually wrong.
say "waiting for the LUT family"
stall=0
last=""
while true; do
  missing=""
  for n in $EXPECTED; do [ -f "$LUTS/$n.npy" ] || missing="$missing $n"; done
  [ -z "$missing" ] && break
  now="$(ls "$LUTS" 2>/dev/null | wc -l)-$(wc -c < build/camcal/lut_builds.log 2>/dev/null || echo 0)"
  if [ "$now" = "$last" ]; then
    stall=$((stall + 1))
    if [ "$stall" -ge 30 ]; then
      say "ABORT: no progress for 30 minutes; still missing:$missing"
      exit 1
    fi
  else
    stall=0
    last="$now"
  fi
  sleep 60
done
say "all LUTs present"

say "building the plan"
"$PY" tools/camcal/lut_plan.py >>"$LOG" 2>&1
captures=$(grep -c '"tag"' "$PLAN")
say "plan has $captures captures"

say "capturing (about $((captures * 34 / 60)) minutes)"
mkdir -p "$OUT"
for attempt in 1 2 3 4 5 6; do
  done_before=$(ls "$OUT"/*__shot.jpg 2>/dev/null | wc -l)
  [ "$done_before" -ge "$captures" ] && break
  say "attempt $attempt, $done_before/$captures done"
  "$PY" -u tools/judge_capture.py --plan "$PLAN" --port COM4 --bench-flip180 \
      --outdir "$OUT" >>"$LOG" 2>&1 || say "attempt $attempt exited nonzero"
  done_after=$(ls "$OUT"/*__shot.jpg 2>/dev/null | wc -l)
  [ "$done_after" -le "$done_before" ] && [ "$attempt" -ge 3 ] && {
    say "ABORT: no progress across three attempts at $done_after/$captures"; exit 2; }
done

have=$(ls "$OUT"/*__shot.jpg 2>/dev/null | wc -l)
say "captured $have/$captures"
[ "$have" -ge "$captures" ] || { say "ABORT: incomplete, not building a page"; exit 3; }

say "building the page"
"$PY" tools/judge_pick.py --rate --session "$OUT" --plan "$PLAN" --seed 777 \
    --out "$OUT/lut.html" \
    --note-prompt "What is wrong with the worst one, and what is right about the best one?" \
    >>"$LOG" 2>&1
say "page at $OUT/lut.html"
say "done"
