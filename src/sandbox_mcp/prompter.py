"""Turns a storyboard's narration into a teleprompter you can read from while recording voiceover.

The `say:` fields stay the single source of truth: this reads them rather than keeping a second
copy of the script that would drift the moment a line is reworded.

Output is one self-contained HTML file with every storyboard embedded, so it opens by double-click
with no server and no network. `file://` blocks fetch, and browser JS is never loaded from a CDN.

    python -m sandbox_mcp.prompter [--out prompter.html]
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from .config import Settings
from .storyboard import Storyboard, StoryboardError, load as load_storyboard
from .transcript import cue_duration

# What is happening on screen while a cue is read; shown dim next to the line so the reader knows
# what they are describing.
_CONTEXT: dict[str, Any] = {
    "run_in_guest": lambda a: str(a.get("command", "")).strip(),
    "answer_guest": lambda a: f"answers {a.get('expect', '')} with {a.get('send') or a.get('keys')}",
    "await_guest": lambda a: f"waits for marker {a.get('marker', '')}",
    "await_desktop": lambda a: "waits for the guest desktop",
    "await_operator": lambda a: str(a.get("message", "")).strip(),
    "type_text": lambda a: f"types {a.get('text', '')!r}",
    "paste_text": lambda a: f"pastes {a.get('text', '')!r}",
    "send_keys": lambda a: f"presses {a.get('chord', '')}",
    "focus_sandbox": lambda a: "brings the sandbox to the front",
}


def _context(action: str, args: dict[str, Any]) -> str:
    render = _CONTEXT.get(action)
    text = " ".join((render(args) if render else "").split())
    return text if len(text) <= 160 else text[:157] + "..."


# What fills the silence between two lines. Deliberately terse - the reader needs to know how long
# to leave, not to re-read the command that is already printed above.
_GAP: dict[str, Any] = {
    "beat": lambda a: f"hold {a.get('seconds', 0)}s",
    "run_in_guest": lambda a: "the command runs",
    "answer_guest": lambda a: f"answer {str(a.get('expect', '')).strip(chr(39))[:40]}",
    "await_guest": lambda a: f"wait for {a.get('marker', '')}",
    "await_desktop": lambda a: "wait for the desktop",
    "await_operator": lambda a: "you do the manual step",
    "focus_sandbox": lambda a: "focus the sandbox",
    "type_text": lambda a: "text is typed",
    "paste_text": lambda a: "text is pasted",
    "send_keys": lambda a: f"press {a.get('chord', '')}",
}


def _gap(action: str, args: dict[str, Any]) -> str:
    render = _GAP.get(action)
    return render(args) if render else action


def cues_for(board: Storyboard) -> list[dict[str, Any]]:
    """Narration in reading order, with what happens between one line and the next."""
    cues: list[dict[str, Any]] = []
    pending_gap: list[str] = []

    def flush_gap() -> None:
        if cues and pending_gap:
            cues[-1]["then"] = pending_gap.copy()
        pending_gap.clear()

    for step in board.steps:
        context = _context(step.action, step.args)
        if step.say:
            flush_gap()
            cues.append({
                "step": step.index,
                "action": step.action,
                "when": "before",
                "text": step.say,
                "context": context,
                "estimate": round(cue_duration(step.say), 1),
                "then": [],
            })
        pending_gap.append(_gap(step.action, step.args))
        if step.say_after:
            flush_gap()
            cues.append({
                "step": step.index,
                "action": step.action,
                "when": "after",
                "text": step.say_after,
                "context": context,
                "estimate": round(cue_duration(step.say_after), 1),
                "then": [],
            })
    flush_gap()
    return cues


def collect(settings: Settings) -> list[dict[str, Any]]:
    boards = []
    for path in settings.list_storyboards():
        try:
            board = load_storyboard(path, settings.resolve_storyboard)
        except StoryboardError:
            continue
        cues = cues_for(board)
        if not cues:
            continue
        boards.append({
            "name": board.name,
            "description": board.description.strip(),
            "steps": len(board.steps),
            "cues": cues,
            "estimate": round(sum(c["estimate"] for c in cues), 1),
        })
    boards.sort(key=lambda b: b["name"])
    return boards


def render(boards: list[dict[str, Any]]) -> str:
    # A "</script>" anywhere in narration would end the block early, so keep it inert.
    payload = json.dumps(boards, indent=1).replace("</", "<\\/")
    return _TEMPLATE.replace("/*__DATA__*/null", payload)


def build(out: Path | None = None, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    boards = collect(settings)
    if not boards:
        raise StoryboardError("No storyboards with `say:` narration were found.")
    target = out or (settings.ensure_work_dir() / "prompter.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(boards), encoding="utf-8")
    return {
        "path": str(target),
        "storyboards": [b["name"] for b in boards],
        "cues": sum(len(b["cues"]) for b in boards),
    }


def serve(path: Path, port: int) -> None:
    """Serve the prompter from localhost.

    Microphone capture needs a secure context, and `file://` only counts as one in some browsers.
    Bound to the loopback interface: this is a local authoring aid, not something to expose.
    """
    import functools
    import http.server
    import socketserver
    import webbrowser

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(path.parent))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/{path.name}"
        print(f"serving {url}  (ctrl+c to stop)")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("stopped")


_TEMPLATE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Storyboard prompter</title>
<style>
  :root {
    --bg: #0b0d10; --fg: #f2f4f8; --dim: #8b96a5; --accent: #4ea1ff; --warn: #d08a3a;
    --line: #1e242c; --size: 42px;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 16px/1.5 "Segoe UI", system-ui, sans-serif; height: 100vh; overflow: hidden;
         display: flex; flex-direction: column; }
  header { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
           padding: 10px 16px; border-bottom: 1px solid var(--line); background: #11151a; }
  select, button, input[type=number] {
    background: #1a2029; color: var(--fg); border: 1px solid #2a323d; border-radius: 6px;
    padding: 6px 10px; font: inherit; }
  button { cursor: pointer; }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled, input:disabled { opacity: .4; cursor: not-allowed; }
  button.on { background: var(--accent); color: #06121f; border-color: var(--accent); }
  button.rec { background: #c8402f; color: #fff; border-color: #c8402f; }
  label { color: var(--dim); display: flex; align-items: center; gap: 6px; white-space: nowrap; }
  #stage { position: relative; flex: 1; overflow: hidden; }
  #scroll { position: absolute; inset: 0; overflow-y: auto; scrollbar-width: none; }
  #scroll::-webkit-scrollbar { display: none; }
  #script { padding: 45vh 8vw; font-size: var(--size); }
  body.mirror #script { transform: scaleX(-1); }
  .cue { margin: 0 0 1.1em; opacity: .32; transition: opacity .2s; }
  .cue.live { opacity: 1; }
  .cue .meta { font-size: .3em; color: var(--dim); letter-spacing: .04em; text-transform: uppercase; }
  .cue .ctx { font-size: .32em; color: var(--dim); font-family: Consolas, monospace;
              margin-top: .3em; word-break: break-word; }
  .gap { font-size: .30em; color: var(--warn); margin: .5em 0 1em; font-family: Consolas, monospace; }
  #line { position: absolute; left: 0; right: 0; top: 45vh; border-top: 2px solid var(--accent);
          opacity: .35; pointer-events: none; }

  /* Paged mode: one cue at a time, centred, for recording take by take. */
  body.paged #scroll { overflow: hidden; }
  body.paged #line { display: none; }
  body.paged #script { padding: 0; height: 100%; display: flex; align-items: center;
                       justify-content: center; }
  body.paged .cue, body.paged .gap { display: none; }
  body.paged .cue.page { display: block; opacity: 1; max-width: 84vw; }
  body.paged .gap.page { display: block; max-width: 84vw; text-align: center; }

  #hud { padding: 6px 16px; border-top: 1px solid var(--line); background: #11151a;
         color: var(--dim); display: flex; gap: 18px; flex-wrap: wrap; align-items: center; }
  #count { position: absolute; inset: 0; display: none; align-items: center; justify-content: center;
           background: rgba(11,13,16,.94); font-size: 22vh; color: var(--accent); }
  #count.on { display: flex; }
  #dot { width: 10px; height: 10px; border-radius: 50%; background: #c8402f; display: none; }
  #dot.on { display: inline-block; animation: pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity: .25; } }
  kbd { background: #1a2029; border: 1px solid #2a323d; border-radius: 4px; padding: 0 5px; }
  #warn { color: var(--warn); }
</style>

<header>
  <select id="pick"></select>
  <select id="mode">
    <option value="scroll">Scroll</option>
    <option value="paged">Paged</option>
  </select>
  <button id="play">Play</button>
  <button id="restart">Restart</button>
  <label><input id="auto" type="checkbox" checked> auto-pace</label>
  <label>speed <input id="speed" type="number" value="40" min="5" max="300" step="5" style="width:5em"></label>
  <label>size <input id="size" type="number" value="42" min="16" max="120" step="2" style="width:4.5em"></label>
  <button id="mirror">Mirror</button>
  <button id="record" title="Record narration through the microphone">Record</button>
  <button id="tap">Tap</button>
  <button id="export">Export VTT</button>
</header>

<div id="stage">
  <div id="scroll"><div id="script"></div></div>
  <div id="line"></div>
  <div id="count"></div>
</div>

<div id="hud">
  <span id="dot"></span>
  <span id="where">-</span>
  <span id="clock">00:00</span>
  <span id="est">-</span>
  <span id="warn"></span>
  <span><kbd>space</kbd> play/next <kbd>&uarr;</kbd>/<kbd>&darr;</kbd> speed <kbd>t</kbd> tap
        <kbd>r</kbd> restart <kbd>m</kbd> mirror</span>
</div>

<script>
const BOARDS = /*__DATA__*/null;
const READ_LINE = 0.45;      // matches #line { top: 45vh }
const GAP_PAUSE = 1.2;       // seconds of silence given to a "then:" marker when auto-pacing

const $ = (id) => document.getElementById(id);
const scroll = $("scroll"), script = $("script");

let board = null, playing = false, raf = null, lastFrame = null;
let startedAt = null, taps = [], plan = null, page = 0;
let recorder = null, chunks = [], audioUrl = null;

const fmt = (s) => {
  s = Math.max(0, s);
  return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(Math.floor(s % 60)).padStart(2, "0");
};
const vtt = (s) => {
  s = Math.max(0, s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${(s % 60).toFixed(3).padStart(6, "0")}`;
};
const paged = () => $("mode").value === "paged";
const elapsed = () => (startedAt === null ? 0 : (performance.now() - startedAt) / 1000);

function load(name) {
  board = BOARDS.find((b) => b.name === name) || BOARDS[0];
  script.innerHTML = "";
  board.cues.forEach((cue, i) => {
    const el = document.createElement("div");
    el.className = "cue";
    el.dataset.index = i;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `step ${cue.step} \\u00b7 ${cue.when} ${cue.action} \\u00b7 ~${cue.estimate}s`;
    const body = document.createElement("div");
    body.textContent = cue.text;
    el.append(meta, body);
    if (cue.context) {
      const ctx = document.createElement("div");
      ctx.className = "ctx";
      ctx.textContent = cue.context;
      el.append(ctx);
    }
    script.append(el);
    if (cue.then && cue.then.length) {
      const gap = document.createElement("div");
      gap.className = "gap";
      gap.dataset.after = i;
      gap.textContent = "\\u23f8 then: " + cue.then.join(" \\u00b7 ");
      script.append(gap);
    }
  });
  $("est").textContent = `${board.cues.length} cues \\u00b7 ~${fmt(board.estimate)} of speech`;
  restart();
}

/* Anchor every cue to the moment it should be spoken, then scroll between anchors. A constant
   pixel rate drifts, because a cue's height and its spoken length are not proportional. */
function buildPlan() {
  const cues = [...document.querySelectorAll(".cue")];
  const offset = innerHeight * READ_LINE;
  const at = [], top = [];
  let t = 0;
  cues.forEach((el, i) => {
    at.push(t);
    top.push(el.offsetTop - offset);
    t += board.cues[i].estimate;
    if (board.cues[i].then && board.cues[i].then.length) t += GAP_PAUSE;
  });
  at.push(t);
  top.push(scroll.scrollHeight - scroll.clientHeight);
  plan = { at, top, total: t };
}

function scrollFor(time) {
  if (!plan) buildPlan();
  const { at, top } = plan;
  if (time <= at[0]) return top[0];
  for (let i = 0; i < at.length - 1; i++) {
    if (time < at[i + 1]) {
      const span = at[i + 1] - at[i] || 1;
      return top[i] + ((top[i + 1] - top[i]) * (time - at[i])) / span;
    }
  }
  return top[top.length - 1];
}

function highlight() {
  if (paged()) return;
  const mark = scroll.getBoundingClientRect().top + innerHeight * READ_LINE;
  // The live cue is the last one to have reached the bar, not the one straddling it: when
  // auto-pacing, a cue sits exactly on the bar at the moment it is due, and a containment test
  // misses it by a fraction of a pixel.
  let live = null;
  document.querySelectorAll(".cue").forEach((el) => {
    el.classList.remove("live");
    if (el.getBoundingClientRect().top <= mark + 2) live = el;
  });
  if (live) {
    live.classList.add("live");
    $("where").textContent = `cue ${Number(live.dataset.index) + 1} of ${board.cues.length}`;
  }
}

function tick(now) {
  if (!playing) return;
  if ($("auto").checked) {
    scroll.scrollTop = scrollFor(elapsed());
  } else if (lastFrame !== null) {
    scroll.scrollTop += (Number($("speed").value) * (now - lastFrame)) / 1000;
  }
  lastFrame = now;
  $("clock").textContent = fmt(elapsed());
  highlight();
  if (scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 2) setPlaying(false);
  else raf = requestAnimationFrame(tick);
}

function setPlaying(on) {
  playing = on;
  lastFrame = null;
  $("play").textContent = on ? "Pause" : "Play";
  $("play").classList.toggle("on", on);
  if (on) {
    if (startedAt === null) startedAt = performance.now();
    if ($("auto").checked) buildPlan();
    raf = requestAnimationFrame(tick);
  } else if (raf) cancelAnimationFrame(raf);
}

function showPage(n) {
  const cues = [...document.querySelectorAll(".cue")];
  page = Math.max(0, Math.min(n, cues.length - 1));
  document.querySelectorAll(".cue, .gap").forEach((el) => el.classList.remove("page"));
  cues[page].classList.add("page");
  const gap = document.querySelector(`.gap[data-after="${page}"]`);
  if (gap) gap.classList.add("page");
  $("where").textContent = `cue ${page + 1} of ${cues.length}`;
}

function advance(step) {
  if (startedAt === null) startedAt = performance.now();
  if (step > 0) tap(page);
  showPage(page + step);
  $("clock").textContent = fmt(elapsed());
}

function restart() {
  setPlaying(false);
  scroll.scrollTop = 0;
  startedAt = null; taps = []; plan = null; page = 0;
  $("clock").textContent = "00:00";
  if (paged()) showPage(0); else highlight();
}

function applyMode() {
  document.body.classList.toggle("paged", paged());
  $("auto").disabled = paged();
  $("speed").disabled = paged() || $("auto").checked;
  restart();
}

// Tapping records when a line was actually spoken, which is what makes the exported cues line up
// with the audio instead of with an estimate.
function tap(index) {
  if (startedAt === null) startedAt = performance.now();
  const at = elapsed();
  if (index === undefined) {
    const live = document.querySelector(".cue.live");
    index = live ? Number(live.dataset.index) : taps.length;
  }
  if (taps.some((t) => t.index === index)) return;
  taps.push({ index, at });
  $("where").textContent = `tapped cue ${index + 1} at ${fmt(at)}`;
}

function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function cueTimes() {
  if (taps.length) return taps.slice().sort((a, b) => a.at - b.at);
  if (!plan) buildPlan();
  return board.cues.map((_, i) => ({ index: i, at: plan.at[i] }));
}

function exportVtt() {
  const times = cueTimes();
  const lines = ["WEBVTT", ""];
  times.forEach((t, i) => {
    const cue = board.cues[t.index];
    if (!cue) return;
    const next = i + 1 < times.length ? times[i + 1].at : t.at + cue.estimate;
    lines.push(`${vtt(t.at)} --> ${vtt(Math.max(Math.min(next, t.at + cue.estimate), t.at + 0.5))}`,
               cue.text, "");
  });
  download(new Blob([lines.join("\\n")], { type: "text/vtt" }), board.name + ".vtt");
}

async function toggleRecord() {
  if (recorder && recorder.state !== "inactive") {
    recorder.stop();
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $("warn").textContent = "Microphone needs a secure context - rerun with --serve.";
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    $("warn").textContent = `Microphone unavailable (${err.name}) - rerun with --serve.`;
    return;
  }
  $("warn").textContent = "";
  chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    $("record").textContent = "Record";
    $("record").classList.remove("rec");
    $("dot").classList.remove("on");
    setPlaying(false);
    if (chunks.length) {
      download(new Blob(chunks, { type: chunks[0].type || "audio/webm" }), board.name + ".webm");
      exportVtt();
    }
  };
  restart();
  countIn(3, () => {
    // Clock, scroll and recorder all start on the same tick, so taps are audio timestamps.
    recorder.start();
    startedAt = performance.now();
    $("record").textContent = "Stop";
    $("record").classList.add("rec");
    $("dot").classList.add("on");
    if (paged()) showPage(0); else setPlaying(true);
  });
}

function countIn(n, done) {
  const el = $("count");
  el.classList.add("on");
  el.textContent = n || "";
  if (n === 0) { el.classList.remove("on"); done(); return; }
  setTimeout(() => countIn(n - 1, done), 1000);
}

BOARDS.forEach((b) => {
  const o = document.createElement("option");
  o.value = b.name;
  o.textContent = `${b.name} (${b.cues.length} cues)`;
  $("pick").append(o);
});
$("pick").onchange = (e) => load(e.target.value);
$("mode").onchange = applyMode;
$("auto").onchange = () => { $("speed").disabled = $("auto").checked; plan = null; };
$("play").onclick = () => {
  if (paged()) return advance(1);
  playing ? setPlaying(false) : countIn(3, () => setPlaying(true));
};
$("restart").onclick = restart;
$("tap").onclick = () => tap();
$("export").onclick = exportVtt;
$("record").onclick = toggleRecord;
$("mirror").onclick = (e) => {
  document.body.classList.toggle("mirror");
  e.target.classList.toggle("on", document.body.classList.contains("mirror"));
};
$("size").oninput = (e) => {
  document.documentElement.style.setProperty("--size", e.target.value + "px");
  plan = null;
};
scroll.onscroll = highlight;
addEventListener("resize", () => { plan = null; });

addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT"].includes(e.target.tagName)) return;
  const speed = $("speed");
  if (e.code === "Space") { e.preventDefault(); $("play").click(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); if (paged()) advance(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); if (paged()) advance(-1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); speed.value = Number(speed.value) + 5; }
  else if (e.key === "ArrowDown") { e.preventDefault(); speed.value = Math.max(5, Number(speed.value) - 5); }
  else if (e.key === "t") tap();
  else if (e.key === "r") restart();
  else if (e.key === "m") $("mirror").click();
});

$("speed").disabled = true;
load(BOARDS[0].name);
</script>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="where to write the HTML")
    parser.add_argument(
        "--serve",
        nargs="?",
        type=int,
        const=8765,
        default=None,
        metavar="PORT",
        help="serve on localhost and open a browser, so microphone recording is allowed",
    )
    args = parser.parse_args()
    result = build(args.out)
    print(f"{result['path']}  ({len(result['storyboards'])} storyboards, {result['cues']} cues)")
    for name in result["storyboards"]:
        print(f"  {name}")
    if args.serve:
        serve(Path(result["path"]), args.serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


