#!/usr/bin/env python3
"""Rate single panel captures on an absolute scale, with room to say why.

Pairwise comparison answers "which of these two", and that is the right question
for fitting a ranking — but it structurally cannot express "this one just looks
good". A forced choice between two mediocre renders records a preference where
the honest answer is that neither is worth shipping, and a rare genuinely good
result looks identical to a marginal win.

So this shows one photographed candidate at a time and asks for an absolute
verdict on a five-point scale, plus free text. Two things come out of that which
pairwise judging cannot give:

**An acceptability floor.** Ratings are on a fixed scale, so "good" means the
same thing on every image. That supports "never ship anything below ok", which a
ranking cannot express at all.

**Reasons.** The note box is the only channel in this whole apparatus that can
say *why* something is wrong. Every metric in the bank was somebody's guess at
what matters; a sentence like "faces went waxy" is what suggests the metric that
was missing.

Ratings are ordinal, not interval — the gap from terrible to bad is not
necessarily the gap from ok to good — so they are fitted with ordinal regression
downstream rather than treated as numbers.

    python tools/judge_rate.py --session build/camcal/session1 \\
        --plan build/camcal/session40_plan.json --count 120
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Hokku image rating</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#111; color:#ddd; font:14px system-ui,sans-serif; }
 header { padding:8px 16px; display:flex; gap:14px; align-items:center;
          border-bottom:1px solid #333; position:sticky; top:0; background:#111; z-index:3 }
 .grow { flex:1 }
 button { background:#222; color:#ddd; border:1px solid #444; border-radius:5px;
          padding:8px 14px; font-size:14px; cursor:pointer }
 button:hover { background:#2c2c2c }
 kbd { background:#000; border:1px solid #444; border-radius:3px; padding:1px 5px }
 #wrap { display:flex; gap:12px; padding:12px; align-items:flex-start }
 #wrap img { max-width:100%; border:1px solid #333; border-radius:4px }
 figure { margin:0; flex:1; text-align:center }
 figcaption { color:#888; font-size:12px; padding-top:4px }
 #srcfig { display:none }
 footer { position:sticky; bottom:0; background:#161616; border-top:1px solid #333;
          padding:12px 16px }
 .scale { display:flex; align-items:center; gap:14px; max-width:920px }
 input[type=range] { flex:1; accent-color:#6b9bd1 }
 .ticks { display:flex; justify-content:space-between; max-width:920px;
          color:#888; font-size:12px; margin-top:2px }
 .ticks span { width:20%; text-align:center }
 .ticks span.on { color:#fff; font-weight:600 }
 textarea { width:100%; max-width:920px; margin-top:10px; background:#0d0d0d;
            color:#ddd; border:1px solid #444; border-radius:5px; padding:8px;
            font:13px system-ui,sans-serif; resize:vertical }
 .unrated { color:#c08a5a }
 .done { padding:40px; text-align:center; font-size:16px; line-height:1.7 }
</style>
<header>
  <button onclick="go(-1)"><kbd>&larr;</kbd> Prev</button>
  <button onclick="go(1)">Next <kbd>&rarr;</kbd></button>
  <button onclick="toggleSrc()">Show original <kbd>s</kbd></button>
  <span class="grow"></span>
  <span id="prog"></span>
  <span id="state"></span>
  <button onclick="save()"><b>Export</b></button>
</header>
<div id="wrap">
  <figure><img id="IMG"><figcaption id="cap"></figcaption></figure>
  <figure id="srcfig"><img id="SRC"><figcaption>original file</figcaption></figure>
</div>
<footer>
  <div class="scale">
    <input type="range" id="slider" min="0" max="4" step="1" value="2"
           oninput="setRating(this.value)">
  </div>
  <div class="ticks" id="ticks"></div>
  <textarea id="note" rows="2" placeholder="Optional: what is wrong with it, or right about it?"
            oninput="setNote(this.value)"></textarea>
</footer>
<script>
const ITEMS = __ITEMS__;
const STAMP = "__STAMP__";
const LABELS = ["terrible", "bad", "meh", "ok", "good"];
const KEY = "hokku_rate_" + STAMP;
let data = {}, i = 0;
try { data = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
function persist() { try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (e) {} }
function cur() { return ITEMS[i]; }
function rec() { return data[cur().tag] || {}; }
function drawTicks() {
  const r = rec();
  document.getElementById("ticks").innerHTML = LABELS.map((l, n) =>
    '<span class="' + (r.rating === n ? "on" : "") + '">' + l + "</span>").join("");
}
function show() {
  if (i >= ITEMS.length) {
    document.getElementById("wrap").innerHTML =
      '<div class="done">All ' + ITEMS.length + ' rated.<br>' +
      'Click <b>Export</b> and give the file to Claude.</div>';
    return;
  }
  const t = cur(), r = rec();
  document.getElementById("IMG").src = t.view;
  document.getElementById("SRC").src = "source__" + t.image + ".png";
  document.getElementById("cap").textContent = t.image;
  // Default to the middle only when unrated, so scrolling back does not
  // silently overwrite a real verdict with "meh".
  document.getElementById("slider").value = r.rating === undefined ? 2 : r.rating;
  document.getElementById("note").value = r.note || "";
  document.getElementById("prog").textContent = (i + 1) + "/" + ITEMS.length;
  const n = Object.values(data).filter(d => d.rating !== undefined).length;
  document.getElementById("state").innerHTML = r.rating === undefined
    ? '<span class="unrated">unrated &middot; ' + n + " done</span>"
    : "<b>" + LABELS[r.rating] + "</b> &middot; " + n + " done";
  drawTicks();
}
function setRating(v) {
  const t = cur().tag;
  data[t] = Object.assign({}, data[t], { rating: parseInt(v, 10), t: Date.now() });
  persist(); show();
}
function setNote(v) {
  const t = cur().tag;
  data[t] = Object.assign({}, data[t], { note: v, t: Date.now() });
  persist();
  const n = Object.values(data).filter(d => d.rating !== undefined).length;
  document.getElementById("state").innerHTML =
    (rec().rating === undefined ? '<span class="unrated">unrated</span>' :
     "<b>" + LABELS[rec().rating] + "</b>") + " &middot; " + n + " done";
}
function go(d) { i = Math.max(0, Math.min(ITEMS.length, i + d)); show(); }
function toggleSrc() {
  const f = document.getElementById("srcfig");
  f.style.display = f.style.display === "block" ? "none" : "block";
}
function save() {
  const blob = new Blob([JSON.stringify({ stamp: STAMP, ratings: data }, null, 1)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ratings.json";
  a.click();
}
addEventListener("keydown", (e) => {
  if (e.target.tagName === "TEXTAREA") return;
  if (e.key === "ArrowLeft") go(-1);
  else if (e.key === "ArrowRight") go(1);
  else if (e.key === "s") toggleSrc();
  else if (e.key >= "1" && e.key <= "5") {
    document.getElementById("slider").value = +e.key - 1;
    setRating(+e.key - 1);
  }
});
show();
</script>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session1"))
    ap.add_argument("--plan", type=Path, default=Path("build/camcal/session40_plan.json"))
    ap.add_argument("--count", type=int, default=0, help="0 = every captured candidate")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))

    # Make the cropped views this page needs, rather than depending on the
    # pairwise tool having been run over the same plan. The crop removes the
    # black border the rectification leaves where the camera did not quite cover
    # the panel; it is measured from the captures so it follows the rig.
    import cv2  # noqa: PLC0415

    from judge_session import common_valid_box  # noqa: PLC0415 — only this path

    captured = [
        c["tag"]
        for c in plan["candidates"]
        if (args.session / f"{c['tag']}__measured.png").exists()
    ]
    todo = [t for t in captured if not (args.session / f"{t}__view.png").exists()]
    if todo:
        x0, y0, x1, y1 = common_valid_box(args.session, captured)
        print(f"  cropping {len(todo)} new captures to ({x0},{y0})-({x1},{y1})")
        for tag in todo:
            img = cv2.imread(str(args.session / f"{tag}__measured.png"))
            if img is not None:
                cv2.imwrite(str(args.session / f"{tag}__view.png"), img[y0:y1, x0:x1])

    # And the source references for the "show original" toggle.
    from PIL import Image  # noqa: PLC0415

    from cam_compare import source_canvas  # noqa: PLC0415
    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415

    display = DISPLAY_REGISTRY[plan.get("model", "huessen_epf1301")]
    for name in sorted({c["image_name"] for c in plan["candidates"]}):
        out_src = args.session / f"source__{name}.png"
        if out_src.exists():
            continue
        entry = next(c for c in plan["candidates"] if c["image_name"] == name)
        canvas = source_canvas(Path(entry["image"]), display, "x")
        x0, y0, x1, y1 = common_valid_box(args.session, captured)
        Image.fromarray(canvas[y0:y1, x0:x1]).save(out_src)

    items = []
    for entry in plan["candidates"]:
        view = args.session / f"{entry['tag']}__view.png"
        if view.exists():
            items.append(
                {
                    "tag": entry["tag"],
                    "image": entry["image_name"],
                    "view": view.name,
                    "config": entry["config_tag"],
                }
            )
    if not items:
        print(f"  no cropped captures under {args.session} — run judge_session plan first")
        return 1

    # Shuffled, so stopping early still leaves a spread across images and
    # settings rather than everything from the first few photographs. Seeded so
    # the order is reproducible.
    rng = random.Random(args.seed)  # noqa: S311 — presentation order, not secrecy
    rng.shuffle(items)
    if args.count:
        items = items[: args.count]

    stamp = hashlib.sha256(json.dumps([x["tag"] for x in items]).encode()).hexdigest()[:12]
    page = PAGE.replace("__ITEMS__", json.dumps(items)).replace("__STAMP__", stamp)
    out = args.out or args.session / "rate.html"
    out.write_text(page, encoding="utf-8")
    (args.session / "rate_key.json").write_text(
        json.dumps({"stamp": stamp, "items": items}, indent=1), encoding="utf-8"
    )

    print(f"  {len(items)} captures to rate, over {len({x['image'] for x in items})} images")
    print(f"  stamp {stamp}")
    print(f"  open {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
