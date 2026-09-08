# demo-video-kit

Narrated demo videos built from real material, with a check at the end that refuses to ship
a broken file.

This is the pipeline behind a run of hackathon and bounty submission videos. It exists
because every other way of making them can hand you an MP4 that looks finished and is
missing a third of the narration, and you find out after you have sent it to a judge.

## What it does

1. `narration.txt` is split on blank lines into paragraphs, one per segment.
2. Each paragraph is spoken by a text-to-speech endpoint. **A silent or missing take aborts
   the build.** No silent holes.
3. The visuals are rendered to a page and recorded by a headless browser. Each segment is
   held on screen for exactly as long as its line takes to say, so nothing drags and nothing
   is cut off.
4. The takes are muxed back in at the segment offsets.
5. The finished file is **transcribed back** and diffed against the script. Under 85 percent
   word coverage, the build aborts and produces nothing.

Step 5 is the point. Everything upstream can fail quietly. That step cannot.

## What it is not

Not a screen recorder. Nothing is captured from your desktop, so there is no cursor drift,
no notification popping into frame, no retake because you fumbled at second 40. You give it
material that already exists and it assembles the same video every time.

## Install

```
pip install -r requirements.txt
python -m playwright install chromium
```

You also need `ffmpeg` and `ffprobe` on PATH.

Copy `.env.example` to `.env` and fill in two endpoints. Both are OpenAI-compatible, so
OpenAI, Groq, Together, or anything else with the same shape works:

- `TTS_*` — text to speech, `/audio/speech`
- `STT_*` — speech to text, `/audio/transcriptions`, used only for the check in step 5

## Run

```
python demo_video.py examples/terminal/demo.json
```

Voice takes are cached in `voice/` next to the config. Rerunning after a visual tweak
reuses them and costs nothing; delete the folder to re-record the narration.

Before touching the network:

```
python demo_video.py --selftest
```

## Two kinds of source

### `terminal` — replay a real CLI run

Capture the output of your demo to a file, then let the kit type it back into a
terminal-styled window at a pace a human can read. The text is the genuine output of a real
run, which is the whole argument the video is making.

```json
"source": {
  "type": "terminal",
  "run": "run.txt",
  "section_regex": "^\\[\\d+\\]",
  "tail_marker": "=====",
  "intro_line": "$ npm run t3:demo"
}
```

`section_regex` marks where each section begins. Every section gets one narration
paragraph, in order. `intro_line` prepends a synthetic first screen showing the command
being run; `tail_marker` splits off a final section, usually a summary or receipt. ANSI
colours in the transcript are preserved. `highlight` colours literal strings the run does
not colour itself:

```json
"highlight": { "PAY": "g", "REFUSE": "r" }
```

Classes are `g` green, `r` red, `y` yellow, `c` blue, `b` bold white, `dim` grey.

### `slides` — real screenshots with burned-in captions

For anything with a UI. Each screenshot is held for its line, with a caption across the
bottom, so it reads on a muted timeline.

```json
"source": {
  "type": "slides",
  "dir": "shots",
  "segments": [
    { "image": "01.png", "caption": "An email that gives the AI orders" },
    { "image": "02.png", "caption": "Reported, not obeyed" }
  ]
}
```

The example ships two placeholder images so it runs before you supply your own.

## Config reference

| Key | Default | Meaning |
| --- | --- | --- |
| `title` | `demo` | Terminal window title, or page title for slides |
| `output` | `demo.mp4` | Written next to the config |
| `narration` | `narration.txt` | Blank-line separated paragraphs, one per segment |
| `max_seconds` | `180` | Hard limit; the build aborts rather than overrun a submission rule |
| `width` / `height` | `1280` / `720` | Recording size |
| `voice` | from `TTS_VOICE` | Voice id for your TTS provider |
| `lead` / `tail` | `1.2` / `2.0` | Silence before the first line and after the last |
| `min_segment_seconds` | `2.5` | Floor, so a three word line still gets a readable beat |
| `gap_after_speech` | `0.9` | Breath after each line |
| `min_coverage` | `0.85` | Step 5 threshold |

## The rule that matters

The number of narration paragraphs must equal the number of segments. If they do not, the
build stops and tells you both counts rather than letting the audio drift off the picture.
That mismatch is the single most common mistake when editing one and forgetting the other.
