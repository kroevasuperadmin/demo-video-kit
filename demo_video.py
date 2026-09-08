"""Build a narrated demo video from real material. `python demo_video.py demo.json`

The pipeline, in order:

  1. narration.txt is split on blank lines into paragraphs, one per segment
  2. each paragraph is spoken by a TTS endpoint; a silent or missing take aborts the build
  3. the visuals are rendered to a page and recorded by Playwright, each segment held for
     exactly as long as its narration takes to say
  4. the takes are muxed back in at the segment offsets
  5. the finished file is transcribed BACK and diffed against the script; if the words are
     not audible in the output the build aborts

Step 5 is the reason this exists. Every other way of making these videos can produce a file
that looks finished and is missing a third of the narration, and you find out after you
send it. Here a bad build fails loudly and produces nothing.

Two visual sources are supported, chosen by "source.type" in the config:

  terminal  replay a real CLI transcript in a terminal-styled window. Nothing is captured
            from the desktop; the text is the genuine output of a real run, replayed at a
            pace a human can read.
  slides    a sequence of real screenshots with burned-in captions, each held for its line.

Self-check: `python demo_video.py --selftest` exercises the timing and text logic with no
network, no ffmpeg and no browser.
"""

import asyncio
import html
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- config


def load_env():
    """Environment first, then a local .env. Nothing is ever read from a global path."""
    env = dict(os.environ)
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("\"'"))
    return env


DEFAULTS = {
    "output": "demo.mp4",
    "title": "demo",
    "width": 1280,
    "height": 720,
    "max_seconds": 180,
    "lead": 1.2,
    "tail": 2.0,
    "min_coverage": 0.85,
    "min_segment_seconds": 2.5,
    "gap_after_speech": 0.9,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(io.open(path, encoding="utf-8").read()))
    cfg["_dir"] = os.path.dirname(os.path.abspath(path))
    for key in ("output", "narration"):
        cfg[key] = os.path.join(cfg["_dir"], cfg.get(key, DEFAULTS.get(key, "narration.txt")))
    cfg["_work"] = os.path.join(cfg["_dir"], "work")
    cfg["_voice"] = os.path.join(cfg["_dir"], "voice")
    return cfg


# --------------------------------------------------------------------------- shell


def sh(cmd, check=True, timeout=900):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise SystemExit("ABORT: %s\n%s" % (" ".join(cmd[:4]), (r.stderr or r.stdout)[-800:]))
    return r


def duration(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path])
    return float(r.stdout.strip())


# --------------------------------------------------------------------------- narration


def paragraphs(path):
    text = io.open(path, encoding="utf-8").read()
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def speak(env, cfg, text, path):
    """One TTS call against an OpenAI-compatible /audio/speech endpoint."""
    base = env.get("TTS_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = json.dumps({
        "model": env.get("TTS_MODEL", "tts-1"),
        "input": text,
        "voice": cfg.get("voice", env.get("TTS_VOICE", "alloy")),
        "response_format": "wav",
    }).encode()
    req = urllib.request.Request(base + "/audio/speech", data=body, headers={
        "Authorization": "Bearer " + env["TTS_API_KEY"],
        "content-type": "application/json",
        "User-Agent": "demo-video-kit/1",
    })
    with urllib.request.urlopen(req, timeout=240) as r:
        data = r.read()
    open(path, "wb").write(data)
    return duration(path)


def narrate(env, cfg, paras):
    """Return one duration per paragraph. A silent take is fatal, never skipped."""
    os.makedirs(cfg["_voice"], exist_ok=True)
    out = []
    for i, p in enumerate(paras):
        path = os.path.join(cfg["_voice"], "p%02d.wav" % i)
        secs = 0.0
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            secs = duration(path)          # reuse a good take; this is the expensive step
        else:
            for attempt in range(4):
                try:
                    secs = speak(env, cfg, p, path)
                    if secs > 0.5:
                        break
                except Exception as e:                      # noqa: BLE001 - retry anything
                    print("  retry %d on paragraph %d: %s" % (attempt + 1, i, type(e).__name__))
                time.sleep(8 * (attempt + 1))
        if not secs or secs < 0.5:
            raise SystemExit(
                "ABORT: paragraph %d produced no audio. Refusing to build a video with a "
                "silent hole in it." % i)
        out.append(secs)
        print("  %02d %5.1fs  %s" % (i, secs, p[:58]))
    print("spoken total %.1fs" % sum(out))
    return out


# --------------------------------------------------------------------------- timing


def timeline(cfg, spoken):
    """Segment start offsets and total length. Each segment lasts as long as its line."""
    offsets, t = [], cfg["lead"]
    dwells = []
    for secs in spoken:
        dwell = max(secs + cfg["gap_after_speech"], cfg["min_segment_seconds"])
        offsets.append(t)
        dwells.append(dwell)
        t += dwell
    return offsets, dwells, t + cfg["tail"]


# --------------------------------------------------------------------------- terminal


ANSI = {"2": "dim", "1": "b", "32": "g", "33": "y", "31": "r", "36": "c"}


def ansi_to_html(line):
    """Convert a line of real ANSI output into spans. Unrecognised codes are dropped."""
    out, pos, open_spans = [], 0, 0
    for m in re.finditer(r"\033\[([0-9;]*)m", line):
        out.append(html.escape(line[pos:m.start()]))
        code = m.group(1)
        if code in ("0", ""):
            out.append("</span>" * open_spans)
            open_spans = 0
        elif code in ANSI:
            out.append('<span class="%s">' % ANSI[code])
            open_spans += 1
        pos = m.end()
    out.append(html.escape(line[pos:]))
    out.append("</span>" * open_spans)
    return "".join(out) or "&nbsp;"


def highlight(line_html, rules):
    """Colour literal words the config names, e.g. {"REFUSE": "r", "PAY": "g"}."""
    for word, cls in rules.items():
        safe = html.escape(word)
        if safe in line_html:
            line_html = line_html.replace(safe, '<span class="%s">%s</span>' % (cls, safe), 1)
    return line_html


def terminal_sections(cfg, n_paragraphs):
    """Split a real run transcript into one section per narration paragraph."""
    src = cfg["source"]
    raw = io.open(os.path.join(cfg["_dir"], src["run"]), encoding="utf-8",
                  errors="replace").read().split("\n")
    marker = re.compile(src.get("section_regex", r"^\[\d+\]"))
    starts = [i for i, l in enumerate(raw) if marker.match(l)]
    if not starts:
        raise SystemExit("ABORT: section_regex %r matched nothing in %s"
                         % (src.get("section_regex"), src["run"]))

    tail = src.get("tail_marker")
    tail_at = len(raw)
    if tail:
        hits = [i for i, l in enumerate(raw) if l.startswith(tail)]
        if hits:
            tail_at = hits[0]

    bounds = []
    intro = src.get("intro_line")
    if intro:
        bounds.append(None)                       # synthetic first screen, e.g. the prompt
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else tail_at
        bounds.append((s, e))
    if tail and tail_at < len(raw):
        bounds.append((tail_at, len(raw)))

    if len(bounds) != n_paragraphs:
        raise SystemExit(
            "ABORT: %d screen sections but %d narration paragraphs. They must match one to "
            "one, or the audio drifts off the picture. Sections found at lines %s."
            % (len(bounds), n_paragraphs, starts[:12]))

    rules = cfg.get("highlight", {})
    sections = []
    for b in bounds:
        lines = [intro] if b is None else list(raw[b[0]:b[1]])
        while lines and not lines[-1].strip():
            lines.pop()
        sections.append([highlight(ansi_to_html(l), rules) for l in (lines or [""])])
    return sections


TERMINAL_PAGE = """<!doctype html><meta charset="utf-8"><title>__TITLE__</title>
<style>
  html,body{margin:0;height:100%;background:#0d1117;}
  body{display:flex;align-items:center;justify-content:center;
       font-family:'Cascadia Code','Consolas',monospace;}
  .win{width:__WINW__px;height:__WINH__px;background:#0d1117;border:1px solid #222a33;
       border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.6);overflow:hidden;
       display:flex;flex-direction:column;}
  .bar{height:34px;background:#161b22;display:flex;align-items:center;padding:0 12px;gap:7px;
       border-bottom:1px solid #222a33;flex:0 0 auto;}
  .dot{width:11px;height:11px;border-radius:50%;}
  .t{margin-left:10px;color:#7d8590;font-size:12px;}
  #s{flex:1;padding:14px 18px;overflow:hidden;color:#c9d1d9;font-size:14px;line-height:1.48;
     white-space:pre-wrap;word-break:break-word;}
  .dim{color:#6e7681}.b{font-weight:700;color:#f0f6fc}.g{color:#3fb950;font-weight:700}
  .y{color:#d29922}.r{color:#f85149;font-weight:700}.c{color:#58a6ff}
</style>
<div class="win">
  <div class="bar"><span class="dot" style="background:#ff5f57"></span>
  <span class="dot" style="background:#febc2e"></span>
  <span class="dot" style="background:#28c840"></span>
  <span class="t">__TITLE__</span></div>
  <div id="s"></div>
</div>
<script>
const LINES = __LINES__, LEAD = __LEAD__, TAIL = __TAIL__;
const s = document.getElementById('s');
window.done = false;
(async () => {
  await new Promise(r => setTimeout(r, LEAD));
  for (const line of LINES) {
    const d = document.createElement('div');
    d.innerHTML = line.h;
    s.appendChild(d);
    s.scrollTop = s.scrollHeight;
    await new Promise(r => setTimeout(r, line.d));
  }
  await new Promise(r => setTimeout(r, TAIL));
  window.done = true;
})();
</script>"""


def terminal_page(cfg, sections, dwells):
    """Reveal each section's lines inside its dwell, then hold on the finished screen."""
    lines_out = []
    for lines, dwell in zip(sections, dwells):
        n = len(lines)
        reveal = min(dwell * 0.55, 0.5 + 0.42 * n)
        per = reveal / n
        for j, h in enumerate(lines):
            d = per if j < n - 1 else (dwell - reveal + per)
            lines_out.append({"h": h, "d": int(d * 1000)})
    page = (TERMINAL_PAGE
            .replace("__LINES__", json.dumps(lines_out))
            .replace("__LEAD__", str(int(cfg["lead"] * 1000)))
            .replace("__TAIL__", str(int(cfg["tail"] * 1000)))
            .replace("__WINW__", str(cfg["width"] - 100))
            .replace("__WINH__", str(cfg["height"] - 56))
            .replace("__TITLE__", html.escape(cfg["title"])))
    return page


# --------------------------------------------------------------------------- slides


SLIDES_PAGE = """<!doctype html><meta charset="utf-8"><title>__TITLE__</title>
<style>
  html,body{margin:0;height:100%;background:#0b0d10;overflow:hidden;}
  #f{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;}
  #f img{max-width:100%;max-height:100%;display:block;}
  #cap{position:absolute;left:0;right:0;bottom:36px;text-align:center;
       font-family:'Segoe UI',system-ui,sans-serif;font-size:30px;font-weight:600;
       color:#fff;text-shadow:0 2px 14px rgba(0,0,0,.85);padding:0 60px;line-height:1.3;}
</style>
<div id="f"><img id="im"></div><div id="cap"></div>
<script>
const SEGS = __SEGS__, LEAD = __LEAD__, TAIL = __TAIL__;
const im = document.getElementById('im'), cap = document.getElementById('cap');
window.done = false;
(async () => {
  await new Promise(r => setTimeout(r, LEAD));
  for (const s of SEGS) {
    im.src = s.src; cap.textContent = s.caption || '';
    await new Promise(r => setTimeout(r, s.d));
  }
  await new Promise(r => setTimeout(r, TAIL));
  window.done = true;
})();
</script>"""


def slides_page(cfg, dwells):
    segs = cfg["source"]["segments"]
    if len(segs) != len(dwells):
        raise SystemExit("ABORT: %d slides but %d narration paragraphs. They must match."
                         % (len(segs), len(dwells)))
    out = []
    for seg, dwell in zip(segs, dwells):
        src = os.path.join(cfg["_dir"], cfg["source"].get("dir", ""), seg["image"])
        if not os.path.exists(src):
            raise SystemExit("ABORT: slide image missing: " + src)
        out.append({"src": "file:///" + src.replace("\\", "/"),
                    "caption": seg.get("caption", ""),
                    "d": int(dwell * 1000)})
    return (SLIDES_PAGE
            .replace("__SEGS__", json.dumps(out))
            .replace("__LEAD__", str(int(cfg["lead"] * 1000)))
            .replace("__TAIL__", str(int(cfg["tail"] * 1000)))
            .replace("__TITLE__", html.escape(cfg["title"])))


# --------------------------------------------------------------------------- record


async def record(cfg, page_path):
    from playwright.async_api import async_playwright
    size = {"width": cfg["width"], "height": cfg["height"]}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport=size, record_video_dir=cfg["_work"],
                                        record_video_size=size)
        page = await ctx.new_page()
        await page.goto("file:///" + page_path.replace("\\", "/"))
        await page.wait_for_function("window.done === true", timeout=600000)
        video = page.video
        await ctx.close()
        path = await video.path()
        await browser.close()
        return path


def mux(cfg, silent, offsets):
    inputs, filters = [], []
    for k, at in enumerate(offsets):
        inputs += ["-i", os.path.join(cfg["_voice"], "p%02d.wav" % k)]
        ms = int(at * 1000)
        filters.append("[%d:a]adelay=%d|%d[a%d]" % (k + 1, ms, ms, k))
    graph = (";".join(filters) + ";" + "".join("[a%d]" % i for i in range(len(offsets)))
             + "amix=inputs=%d:normalize=0[v]" % len(offsets))
    sh(["ffmpeg", "-y", "-v", "error", "-i", silent] + inputs +
       ["-filter_complex", graph, "-map", "0:v", "-map", "[v]", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", cfg["output"]])


# --------------------------------------------------------------------------- verify


def words(s):
    return set(w.strip(".,:;!?\"'()").lower() for w in s.split() if len(w) > 3)


def coverage(script, heard):
    """Fraction of the script's distinctive words that are audible in the output."""
    want, got = words(script), words(heard)
    missing = sorted(want - got)
    return (1 - len(missing) / max(1, len(want))), missing


def transcribe(env, video):
    """Send the finished file to an OpenAI-compatible /audio/transcriptions endpoint."""
    mp3 = video + ".check.mp3"
    sh(["ffmpeg", "-y", "-v", "error", "-i", video, "-ac", "1", "-ar", "16000", "-b:a", "32k", mp3])
    base = env.get("STT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    b = "----demovideokit"
    body = bytearray()
    for k, v in (("model", env.get("STT_MODEL", "whisper-1")),
                 ("response_format", "text"), ("language", "en")):
        body.extend(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (b, k, v)).encode())
    body.extend(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n"
                 "Content-Type: audio/mpeg\r\n\r\n" % b).encode())
    body.extend(open(mp3, "rb").read())
    body.extend(("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(base + "/audio/transcriptions", data=bytes(body), headers={
        "Authorization": "Bearer " + env["STT_API_KEY"],
        "Content-Type": "multipart/form-data; boundary=" + b,
        "User-Agent": "demo-video-kit/1",
    })
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- build


def build(config_path):
    env = load_env()
    cfg = load_config(config_path)
    os.makedirs(cfg["_work"], exist_ok=True)

    paras = paragraphs(cfg["narration"])
    print("%d narration paragraphs" % len(paras))
    spoken = narrate(env, cfg, paras)
    offsets, dwells, planned = timeline(cfg, spoken)
    print("planned video %.1fs" % planned)
    if planned > cfg["max_seconds"]:
        raise SystemExit("ABORT: planned %.0fs exceeds the %ds limit. Cut narration."
                         % (planned, cfg["max_seconds"]))

    kind = cfg["source"]["type"]
    if kind == "terminal":
        page = terminal_page(cfg, terminal_sections(cfg, len(paras)), dwells)
    elif kind == "slides":
        page = slides_page(cfg, dwells)
    else:
        raise SystemExit("ABORT: unknown source.type %r (terminal or slides)" % kind)

    page_path = os.path.join(cfg["_work"], "replay.html")
    io.open(page_path, "w", encoding="utf-8").write(page)

    webm = asyncio.run(record(cfg, page_path))
    silent = os.path.join(cfg["_work"], "silent.mp4")
    sh(["ffmpeg", "-y", "-v", "error", "-i", webm, "-an", "-c:v", "libx264", "-preset", "medium",
        "-crf", "21", "-pix_fmt", "yuv420p", "-r", "30", silent])
    print("silent video %.1fs" % duration(silent))

    mux(cfg, silent, offsets)

    final_secs = duration(cfg["output"])
    astreams = sh(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                   "stream=codec_name", "-of", "default=nw=1:nk=1", cfg["output"]]).stdout.strip()
    if not astreams:
        raise SystemExit("ABORT: final file has no audio stream")
    if final_secs > cfg["max_seconds"]:
        raise SystemExit("ABORT: final is %.0fs, over the %ds limit"
                         % (final_secs, cfg["max_seconds"]))

    cov, missing = coverage(" ".join(paras), transcribe(env, cfg["output"]))
    print("\nheard back %.0f%% of the script; missing: %s" % (cov * 100, missing[:12]))
    if cov < cfg["min_coverage"]:
        raise SystemExit("ABORT: narration is missing from the final video. Not shipping it.")

    print("\nFINAL  %s" % cfg["output"])
    print("       %.1fs   %.1f MB   audio=%s"
          % (final_secs, os.path.getsize(cfg["output"]) / 1e6, astreams))


# --------------------------------------------------------------------------- self-check


def selftest():
    """The parts that can be wrong silently: timing, ANSI, highlighting, coverage."""
    cfg = dict(DEFAULTS)
    offsets, dwells, total = timeline(cfg, [3.0, 1.0, 6.0])
    assert dwells == [3.9, 2.5, 6.9], dwells                 # short lines get a floor
    assert offsets[0] == 1.2 and abs(offsets[1] - 5.1) < 1e-9, offsets
    assert abs(total - (1.2 + 13.3 + 2.0)) < 1e-9, total

    assert ansi_to_html("plain") == "plain"
    assert ansi_to_html("\033[32mok\033[0m") == '<span class="g">ok</span>'
    assert ansi_to_html("") == "&nbsp;"
    assert ansi_to_html("<b>") == "&lt;b&gt;"                 # never trust the transcript

    assert highlight("send REFUSE now", {"REFUSE": "r"}) == 'send <span class="r">REFUSE</span> now'
    assert highlight("nothing", {"REFUSE": "r"}) == "nothing"

    cov, missing = coverage("alpha beta gamma", "alpha beta gamma")
    assert cov == 1.0 and missing == [], (cov, missing)
    cov, missing = coverage("alpha beta gamma delta", "alpha beta")
    assert missing == ["delta", "gamma"] and abs(cov - 0.5) < 1e-9, (cov, missing)
    cov, _ = coverage("a an the", "")                        # short words are not counted
    assert cov == 1.0, cov

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif len(sys.argv) > 1:
        build(sys.argv[1])
    else:
        raise SystemExit(__doc__)
