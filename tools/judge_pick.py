#!/usr/bin/env python3
"""Show every version of one photograph together; pick an acceptable one, or none.

The two earlier formats each lost something. Pairwise comparison forces a choice
between two mediocre renders and cannot say "neither". Absolute rating on a
shuffled stream spreads thin — 98 ratings over 240 shuffled cells left only six
or seven paired observations per arm, far too few to separate a 0.3-point
difference, so a whole shootout came back inconclusive.

Grouping by photograph fixes both. Every arm for one image is on screen at once,
so the comparison is direct rather than remembered; and "none of these is
acceptable" is a first-class answer, which is the honest verdict for a picture
this panel cannot yet render well. One decision per photograph instead of six
ratings makes forty images a short sitting rather than a long one.

**Blind, properly this time.** Earlier pages were blind only in the sense that no
label was printed — the image filenames still carried the arm name, so anyone
reading the DOM could see which was which. Here the crops are written under opaque
names and the mapping lives only in the key file, which the page never loads.
Arm order is reshuffled per image, so position carries no information either, and
every version is cut with the same box, so framing carries none.

**`--rate`: the same grouping, with a rating on every version.** A pick records
which version won but not by how much, and nothing about the others. That was
enough to find the gamut dial; it is not enough to fit which setting each image
*needs*, which wants the whole curve per photograph — is the winner a rescue or a
hair's breadth, and is the runner-up nearly as good? So every version gets the
same five-point scale the single-capture page uses, judged side by side rather
than remembered across a shuffled stream.

    python tools/judge_pick.py --session build/camcal/session1 \\
        --plan build/camcal/shootout_plan.json
    python tools/judge_pick.py --rate --seed 29 --plan build/camcal/gamut_plan.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

try:
    import hokku.screens  # noqa: F401 — probe importability
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Hokku: pick an acceptable render</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#111; color:#ddd; font:14px system-ui,sans-serif; }
 header { padding:8px 16px; display:flex; gap:14px; align-items:center;
          border-bottom:1px solid #333; position:sticky; top:0; background:#111; z-index:5 }
 .grow { flex:1 }
 button { background:#222; color:#ddd; border:1px solid #444; border-radius:5px;
          padding:8px 14px; font-size:14px; cursor:pointer }
 button:hover { background:#2c2c2c }
 button.none { border-color:#7a3a3a; color:#e0b0b0 }
 button.none:hover { background:#3a2424 }
 button.same { border-color:#4a5a7a; color:#b0c4e0 }
 button.same:hover { background:#242c3a }
 kbd { background:#000; border:1px solid #444; border-radius:3px; padding:1px 5px }
 #grid { display:grid; grid-template-columns:repeat(__COLS__,1fr); gap:8px; padding:10px }
 .cell { position:relative; cursor:pointer; border:3px solid transparent; border-radius:5px }
 .cell img { width:100%; display:block; border-radius:3px }
 .cell:hover { border-color:#555 }
 .cell.sel { border-color:#5aa15a }
 .cell .n { position:absolute; top:5px; left:5px; background:#000a; padding:1px 7px;
            border-radius:3px; font-size:12px; color:#bbb }
 .cell .zoom { position:absolute; top:5px; right:5px; background:#000a; padding:1px 7px;
               border-radius:3px; font-size:12px; color:#bbb }
 #big { position:fixed; inset:0; background:#000e; display:none; z-index:9;
        align-items:center; justify-content:center }
 #big img { max-width:98vw; max-height:98vh }
 .done { padding:40px; text-align:center; font-size:16px; line-height:1.7 }
 #src { display:none; padding:0 10px 10px; text-align:center }
 #src img { max-width:60%; border:1px solid #333; border-radius:4px }
 .noneon { color:#e08a8a; font-weight:600 }
 .sameon { color:#8aa8e0; font-weight:600 }
</style>
<header>
  <button onclick="go(-1)"><kbd>&larr;</kbd></button>
  <button onclick="go(1)"><kbd>&rarr;</kbd></button>
  <b>Click an acceptable one</b>
  <button class="none" onclick="pick(null)">None acceptable <kbd>x</kbd></button>
  <button class="same" onclick="pick('same')">Can't see the difference <kbd>space</kbd></button>
  <button onclick="toggleSrc()">Original <kbd>s</kbd></button>
  <span class="grow"></span>
  <span id="prog"></span>
  <span id="state"></span>
  <button onclick="save()"><b>Export</b></button>
</header>
<div id="grid"></div>
<div id="src"><img id="SRC"><div>original file</div></div>
<div id="big" onclick="this.style.display='none'"><img id="BIG"></div>
<script>
const ITEMS = __ITEMS__;
const STAMP = "__STAMP__";
const KEY = "hokku_pick_" + STAMP;
let data = {}, i = 0;
try { data = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
function persist() { try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (e) {} }
function cur() { return ITEMS[i]; }
function done() { return Object.keys(data).length; }
function show() {
  const grid = document.getElementById("grid");
  if (i >= ITEMS.length) {
    grid.innerHTML = '<div class="done" style="grid-column:1/4">All ' + ITEMS.length +
      ' photographs done.<br>Click <b>Export</b> and give the file to Claude.</div>';
    document.getElementById("prog").textContent = ITEMS.length + "/" + ITEMS.length;
    return;
  }
  const t = cur(), rec = data[t.image];
  grid.innerHTML = t.files.map((f, n) =>
    '<div class="cell' + (rec && rec.pick === n ? ' sel' : '') + '" onclick="pick(' + n + ')">' +
    '<img src="' + f + '">' +
    '<span class="n">' + (n + 1) + '</span>' +
    '<span class="zoom" onclick="event.stopPropagation();big(\\'' + f + '\\')">zoom</span></div>'
  ).join("");
  document.getElementById("SRC").src = t.src;
  document.getElementById("prog").textContent = (i + 1) + "/" + ITEMS.length;
  let s = done() + " done";
  if (rec) {
    const what = rec.pick === null ? '<span class="noneon">none acceptable</span>'
               : rec.pick === "same" ? '<span class="sameon">no visible difference</span>'
               : "picked #" + (rec.pick + 1);
    s = what + " &middot; " + s;
  }
  document.getElementById("state").innerHTML = s;
}
function pick(n) {
  if (i >= ITEMS.length) return;
  data[cur().image] = { pick: n, t: Date.now() };
  persist();
  // Straight on to the next photograph: the decision is made, and paging back
  // is always available if it was a misclick.
  i++; show();
}
function go(d) { i = Math.max(0, Math.min(ITEMS.length, i + d)); show(); }
function big(f) {
  document.getElementById("BIG").src = f;
  document.getElementById("big").style.display = "flex";
}
function toggleSrc() {
  const s = document.getElementById("src");
  s.style.display = s.style.display === "block" ? "none" : "block";
}
function save() {
  const blob = new Blob([JSON.stringify({ stamp: STAMP, picks: data }, null, 1)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "picks.json";
  a.click();
}
addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") go(-1);
  else if (e.key === "ArrowRight") go(1);
  else if (e.key === "x") pick(null);
  else if (e.key === " ") { e.preventDefault(); pick("same"); }
  else if (e.key === "s") toggleSrc();
  else if (e.key >= "1" && e.key <= "9") {
    const n = +e.key - 1;
    if (i < ITEMS.length && n < cur().files.length) pick(n);
  }
});
// Resume where the judging left off rather than at the beginning.
while (i < ITEMS.length && data[ITEMS[i].image]) i++;
show();
</script>
"""

PAGE_RATE = """<!doctype html>
<meta charset="utf-8">
<title>Hokku: rate every version</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#111; color:#ddd; font:14px system-ui,sans-serif; }
 header { padding:8px 16px; display:flex; gap:14px; align-items:center; flex-wrap:wrap;
          border-bottom:1px solid #333; position:sticky; top:0; background:#111; z-index:5 }
 .grow { flex:1 }
 .hint { color:#999 }
 button { background:#222; color:#ddd; border:1px solid #444; border-radius:5px;
          padding:8px 14px; font-size:14px; cursor:pointer }
 button:hover { background:#2c2c2c }
 kbd { background:#000; border:1px solid #444; border-radius:3px; padding:1px 5px }
 #grid { display:grid; grid-template-columns:repeat(__COLS__,1fr); gap:8px; padding:10px }
 .cell { position:relative; border:3px solid transparent; border-radius:5px; background:#171717 }
 .cell.focus { border-color:#6b9bd1 }
 .cell img { width:100%; display:block; border-radius:3px 3px 0 0; cursor:pointer }
 .cell .n { position:absolute; top:5px; left:5px; background:#000a; padding:1px 7px;
            border-radius:3px; font-size:12px; color:#bbb }
 .cell.unrated .n { color:#e0a86a }
 .cell .zoom { position:absolute; top:5px; right:5px; background:#000a; padding:1px 7px;
               border-radius:3px; font-size:12px; color:#bbb; cursor:zoom-in }
 .bar { display:flex; gap:4px; padding:6px }
 .bar button { flex:1; padding:6px 0; font-size:12px }
 .bar button.on { background:#2d4a6b; border-color:#6b9bd1; color:#fff; font-weight:600 }
 #notewrap { padding:0 10px 10px }
 textarea { width:100%; box-sizing:border-box; background:#0d0d0d; color:#ddd;
            border:1px solid #444; border-radius:5px; padding:8px;
            font:13px system-ui,sans-serif; resize:vertical }
 #big { position:fixed; inset:0; background:#000e; display:none; z-index:9;
        align-items:center; justify-content:center }
 #big img { max-width:98vw; max-height:98vh }
 .done { padding:40px; text-align:center; font-size:16px; line-height:1.7 }
 #src { display:none; padding:0 10px 10px; text-align:center }
 #src img { max-width:60%; border:1px solid #333; border-radius:4px }
 .left { color:#e0a86a }
 .full { color:#8ac08a; font-weight:600 }
</style>
<header>
  <button onclick="go(-1)"><kbd>&larr;</kbd></button>
  <button onclick="go(1)"><kbd>&rarr;</kbd></button>
  <span class="hint"><kbd>1</kbd>&ndash;<kbd>5</kbd> rate the outlined one
    &middot; <kbd>space</kbd> all the same as it
    &middot; <kbd>tab</kbd> next &middot; <kbd>z</kbd> zoom</span>
  <button onclick="toggleSrc()">Original <kbd>s</kbd></button>
  <span class="grow"></span>
  <span id="prog"></span>
  <span id="state"></span>
  <button onclick="save()"><b>Export</b></button>
</header>
<div id="grid"></div>
<div id="notewrap"><textarea id="note" rows="2" oninput="setNote(this.value)"
  placeholder="Optional: what separates them, or what is wrong with all of them?"></textarea></div>
<div id="src"><img id="SRC"><div>original file</div></div>
<div id="big" onclick="this.style.display='none'"><img id="BIG"></div>
<script>
const ITEMS = __ITEMS__;
const STAMP = "__STAMP__";
const LABELS = ["terrible", "bad", "meh", "ok", "good"];
const KEY = "hokku_grouprate_" + STAMP;
let data = {}, i = 0, focus = 0, last = -1;
try { data = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
function persist() { try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (e) {} }
function cur() { return ITEMS[i]; }
function rec() {
  const k = cur().image;
  if (!data[k]) data[k] = { r: {}, note: "", same: false };
  return data[k];
}
function rated(it) {
  const d = data[it.image];
  return d ? Object.keys(d.r).length : 0;
}
function complete(it) { return rated(it) >= it.files.length; }
function firstUnrated() {
  const r = (data[cur().image] || { r: {} }).r, n = cur().files.length;
  for (let k = 0; k < n; k++) if (r[k] === undefined) return k;
  return -1;
}
function show() {
  const grid = document.getElementById("grid");
  const note = document.getElementById("note");
  if (i >= ITEMS.length) {
    grid.innerHTML = '<div class="done" style="grid-column:1/-1">All ' + ITEMS.length +
      ' photographs done.<br>Click <b>Export</b> and give the file to Claude.</div>';
    note.style.display = "none";
    document.getElementById("prog").textContent = ITEMS.length + "/" + ITEMS.length;
    document.getElementById("state").innerHTML = "";
    return;
  }
  note.style.display = "";
  const t = cur(), r = (data[t.image] || { r: {} }).r;
  grid.innerHTML = t.files.map((f, n) =>
    '<div class="cell' + (n === focus ? ' focus' : '') + (r[n] === undefined ? ' unrated' : '') +
    '" onclick="setFocus(' + n + ')">' +
    '<img src="' + f + '">' +
    '<span class="n">' + (n + 1) + (r[n] === undefined ? '' : ' &middot; ' + LABELS[r[n]]) + '</span>' +
    '<span class="zoom" onclick="event.stopPropagation();big(' + n + ')">zoom</span>' +
    '<div class="bar">' + LABELS.map((l, v) =>
      '<button class="' + (r[n] === v ? 'on' : '') +
      '" onclick="event.stopPropagation();rate(' + n + ',' + v + ')">' + (v + 1) + ' ' + l +
      '</button>').join("") + '</div></div>'
  ).join("");
  document.getElementById("SRC").src = t.src;
  note.value = (data[t.image] || {}).note || "";
  document.getElementById("prog").textContent = (i + 1) + "/" + ITEMS.length;
  const n = rated(t), full = ITEMS.filter(complete).length;
  document.getElementById("state").innerHTML =
    (n >= t.files.length
      ? '<span class="full">all ' + n + ' rated &mdash; <kbd>&rarr;</kbd> next</span>'
      : '<span class="left">' + n + '/' + t.files.length + ' rated</span>') +
    ' &middot; ' + full + ' of ' + ITEMS.length + ' photographs complete';
}
function setFocus(n) { focus = n; show(); }
function rate(n, v) {
  const d = rec();
  d.r[n] = v;
  last = n;
  // A single change after "all the same" means they are no longer all the same.
  if (d.same && Object.values(d.r).some(x => x !== v)) d.same = false;
  d.t = Date.now();
  persist();
  // Move on to the next version still waiting for a verdict, so a photograph is
  // five keypresses; stay put once every version has one. A zoomed view closes,
  // since it is showing the version just rated rather than the next one.
  const next = firstUnrated();
  if (next >= 0) focus = next;
  document.getElementById("big").style.display = "none";
  show();
}
function allSame() {
  // Rating moves the outline on, so the natural "rate one, then space" would
  // otherwise land on the next, unrated version. Use the one just rated.
  const d = rec(), from = d.r[focus] !== undefined ? focus : last;
  const v = from < 0 ? undefined : d.r[from];
  if (v === undefined) {
    document.getElementById("state").innerHTML =
      '<span class="left">rate the outlined one first, then <kbd>space</kbd></span>';
    return;
  }
  cur().files.forEach((_f, n) => { d.r[n] = v; });
  d.same = true;
  d.t = Date.now();
  persist(); show();
}
function setNote(v) { rec().note = v; rec().t = Date.now(); persist(); }
function go(d) {
  i = Math.max(0, Math.min(ITEMS.length, i + d));
  focus = 0; last = -1;
  if (i < ITEMS.length) { const k = firstUnrated(); if (k >= 0) focus = k; }
  show();
}
function big(n) {
  document.getElementById("BIG").src = cur().files[n];
  document.getElementById("big").style.display = "flex";
}
function toggleSrc() {
  const s = document.getElementById("src");
  s.style.display = s.style.display === "block" ? "none" : "block";
}
function save() {
  const blob = new Blob([JSON.stringify({ stamp: STAMP, ratings: data }, null, 1)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "grouprate.json";
  a.click();
}
addEventListener("keydown", (e) => {
  if (e.target.tagName === "TEXTAREA") {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  const overlay = document.getElementById("big");
  if (e.key === "Escape") { overlay.style.display = "none"; return; }
  if (e.key === "ArrowLeft") go(-1);
  else if (e.key === "ArrowRight" || e.key === "Enter") go(1);
  else if (i >= ITEMS.length) return;
  else if (e.key === "s") toggleSrc();
  else if (e.key === "z") {
    if (overlay.style.display === "flex") overlay.style.display = "none"; else big(focus);
  }
  else if (e.key === "Tab") {
    e.preventDefault();
    const n = cur().files.length;
    setFocus((focus + (e.shiftKey ? n - 1 : 1)) % n);
  }
  else if (e.key === " ") { e.preventDefault(); allSame(); }
  else if (e.key >= "1" && e.key <= "5") rate(focus, +e.key - 1);
});
// Resume at the first photograph not fully rated.
while (i < ITEMS.length && complete(ITEMS[i])) i++;
go(0);
</script>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, default=Path("build/camcal/session1"))
    ap.add_argument("--plan", type=Path, default=Path("build/camcal/shootout_plan.json"))
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument(
        "--arms",
        nargs="*",
        default=None,
        help="only these arms — a two-way test resolves far more than a six-way one",
    )
    ap.add_argument(
        "--rate",
        action="store_true",
        help="rate every version on the five-point scale instead of picking one",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--keep-bars",
        action="store_true",
        help="show letterbox bars instead of cropping to the picture",
    )
    ap.add_argument(
        "--force", action="store_true", help="replace a key file that belongs to another session"
    )
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    by_image: dict[str, list[dict]] = defaultdict(list)
    for entry in plan["candidates"]:
        measured = args.session / f"{entry['tag']}__measured.png"
        if args.arms and entry["config_tag"] not in args.arms:
            continue
        if measured.exists():
            by_image[entry["image_name"]].append(
                {
                    "tag": entry["tag"],
                    "arm": entry["config_tag"],
                    "path": entry["image"],
                    # production plans crop to fill below this zoom (0.14 live)
                    "fill": float(entry.get("crop_to_fill_threshold", 0.0)),
                }
            )

    complete = {k: v for k, v in by_image.items() if len(v) >= 2}
    if not complete:
        print(f"  no captures under {args.session}")
        return 1

    # One crop box for every version on the page, cut fresh from the measured
    # captures. The per-capture `__view.png` crops cannot be reused: each was cut
    # with the box of whichever session first cropped it, and a baseline carried
    # over from an earlier session came out 1329x1000 beside 1371x1025 for the
    # new arms — framed ~3 % tighter, so identifiable at a glance on the very
    # page meant to be blind.
    import cv2  # noqa: PLC0415 — only needed to cut the crops
    import numpy as np  # noqa: PLC0415

    from judge_session import common_valid_box  # noqa: PLC0415

    box = common_valid_box(args.session, [a["tag"] for v in complete.values() for a in v])
    print(f"  valid box ({box[0]},{box[1]})-({box[2]},{box[3]}) shared by every capture")

    # Each photograph is then cropped to its own picture area. The letterbox
    # bars are identical in every version, so they carry no information — but
    # on the glass they are the brightest thing in the frame, and a picture
    # judged inside a wide bright surround looks darker and duller than it is.
    # Every version of one photograph gets the same crop, so this stays blind.
    from cam_compare import source_canvas  # noqa: PLC0415
    from hokku.screens.registry import DISPLAY_REGISTRY  # noqa: PLC0415
    from letterbox import padding_visible, picture_rect  # noqa: PLC0415

    display = DISPLAY_REGISTRY[plan.get("model", "huessen_epf1301")]

    # Opaque per-slot filenames. The captures are named after their arm, so
    # serving them directly would put the answer in the DOM. The crop is part of
    # the name so a crop cut with a different box is never silently reused.
    blind_dir = args.session / "pick"
    blind_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)  # noqa: S311 — presentation order, not secrecy

    items, key = [], []
    for image in sorted(complete):
        arms = complete[image][:]
        crop = box
        rect = None
        if not args.keep_bars:
            padding = padding_visible(Path(arms[0]["path"]), display, arms[0]["fill"])
            rect = picture_rect(padding)
            # Only trust the rectangle if every capture was rendered with it. The
            # renderer forces padding to exactly the white ink, so the rendered
            # frame must be that colour everywhere the geometry says "bar". A
            # capture from before the bench applied EXIF rotation is sideways
            # and fails this, and cropping it would cut the wrong region.
            #
            # Two pixels of slack at the edge: captures made before the bench used
            # production's loader fit a full-size image rather than a pre-shrunk
            # one, and the bar edge lands one pixel apart. The crop is trimmed by
            # the same margin so no sliver of bar is shown either way.
            edge = 2
            white = np.rint(display.palette_measured_rgb[1]).clip(0, 255).astype(np.uint8)[::-1]
            if rect is not None:
                sure_bar = padding.copy()
                sure_bar[
                    max(rect[1] - edge, 0) : rect[3] + edge, max(rect[0] - edge, 0) : rect[2] + edge
                ] = False
                for arm in arms:
                    expected = cv2.imread(str(args.session / f"{arm['tag']}__expected.png"))
                    if expected is None or not (expected[sure_bar] == white).all():
                        print(f"  {image}: capture geometry differs from production — bars kept")
                        rect = None
                        break
            if rect is not None:
                rect = (rect[0] + edge, rect[1] + edge, rect[2] - edge, rect[3] - edge)
        if rect is not None:
            crop = (
                max(box[0], rect[0]),
                max(box[1], rect[1]),
                min(box[2], rect[2]),
                min(box[3], rect[3]),
            )
        x0, y0, x1, y1 = crop
        rng.shuffle(arms)
        slots, files = [], []
        for n, arm in enumerate(arms):
            opaque = hashlib.sha256(f"{args.seed}|{image}|{n}|{crop}".encode()).hexdigest()[:16]
            dest = blind_dir / f"{opaque}.png"
            if not dest.exists():
                img = cv2.imread(str(args.session / f"{arm['tag']}__measured.png"))
                if img is None:
                    raise FileNotFoundError(f"capture missing for {arm['tag']}")
                cv2.imwrite(str(dest), img[y0:y1, x0:x1])
            files.append(f"pick/{dest.name}")
            slots.append({"slot": n, "arm": arm["arm"], "tag": arm["tag"], "file": dest.name})
        # The original, through production's loader and geometry (so upright),
        # with the same crop as the versions beside it.
        src_name = "src_" + hashlib.sha256(f"{image}|{crop}".encode()).hexdigest()[:16] + ".png"
        if not (blind_dir / src_name).exists():
            canvas = source_canvas(Path(arms[0]["path"]), display, "x", arms[0]["fill"])
            cv2.imwrite(
                str(blind_dir / src_name), cv2.cvtColor(canvas[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
            )
        items.append({"image": image, "files": files, "src": f"pick/{src_name}"})
        key.append({"image": image, "crop": list(crop), "slots": slots})

    # Rating mode folds its name into the stamp so a ratings export can never be
    # matched against a pick key built from the same images.
    stamped: list = [(x["image"], x["files"]) for x in items]
    if args.rate:
        stamped = ["rate", *stamped]
    stamp = hashlib.sha256(json.dumps(stamped).encode()).hexdigest()[:12]

    out = args.out or args.session / ("grouprate.html" if args.rate else "pick.html")
    # The key is named after its page. It used to be pick_key.json whatever the
    # page, so building a second page silently replaced the only map from an
    # earlier session's exported verdicts back to the arms they were about.
    key_path = out.with_name(f"{out.stem}_key.json")
    if key_path.exists() and not args.force:
        old = json.loads(key_path.read_text(encoding="utf-8")).get("stamp")
        if old != stamp:
            print(f"  {key_path} holds a different session (stamp {old}).")
            print("  Its verdicts would lose their key. Use another --out, or --force.")
            return 1

    columns = min(max(len(items[0]["files"]), 1), 3)
    page = (
        (PAGE_RATE if args.rate else PAGE)
        .replace("__ITEMS__", json.dumps(items))
        .replace("__STAMP__", stamp)
        .replace("__COLS__", str(columns))
    )
    out.write_text(page, encoding="utf-8")
    key_path.write_text(
        json.dumps(
            {"stamp": stamp, "mode": "rate" if args.rate else "pick", "images": key}, indent=1
        ),
        encoding="utf-8",
    )

    counts = {len(v) for v in complete.values()}
    print(f"  {len(items)} photographs, {sorted(counts)} versions each")
    print(f"  stamp {stamp}")
    # Search the data the page carries, not its fixed template: a short arm name
    # like "ref" otherwise matches the export code's `a.href` and cries wolf.
    data = json.dumps(items)
    leaked = any(s["arm"] in data or s["tag"] in data for k in key for s in k["slots"])
    print(f"  arm names appear in the page: {leaked}")
    print(f"  key {key_path}")
    print(f"  open {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
