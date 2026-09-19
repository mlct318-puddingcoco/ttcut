# ttcut

Turn raw table tennis footage into a tight, scored match video — cuts the ball-chasing, burns in a live scoreboard, and finishes on a match-summary card.

**English** · [繁體中文](README.zh-TW.md)

No account or upload. Your video never leaves your machine.

<img width="1905" height="934" alt="ttcut demo" src="https://github.com/user-attachments/assets/54aef9c3-1198-4d8e-90f7-29a79757e5ff" />


<img width="1377" height="883" alt="image" src="https://github.com/user-attachments/assets/74a01743-9fb1-48c3-ae74-9201e4a50c91" />


![The end-of-film stats board](stats-board.png)

---

## What it does

You tag the rallies once in a browser, and ttcut does the rest:

- **Cuts the dead time.** Everything between a point and the next serve — picking the ball up, walking back, towelling off — is removed. A 40-minute recording usually lands somewhere around 12–15 minutes.
- **Burns in a scoreboard.** Names, games, and the running score sit in the corner for the whole film, derived from your tags rather than typed in by hand.
- **Ends on a summary card.** Optionally freezes the last frame and draws a per-game score table plus three metrics for each player.
- **Marks highlights as you score.** After `A` or `B`, press `H` to flag that completed rally in the same tags JSON for a future tournament compilation.

Tagging is manual on purpose. There is no ball tracking to misfire, and no model to download — you press `S` on the serve and `A`/`B` on the point, which is about as fast as watching the match anyway.

## Requirements

- **Python 3.8 or newer**
- **ffmpeg**, built with `libass` (needed by the `subtitles` filter that draws the scoreboard)

```bash
# macOS
brew install ffmpeg

# Windows — download a build from https://www.gyan.dev/ffmpeg/builds/
# and put ffmpeg.exe next to the script, or pass --ffmpeg "C:\ffmpeg\bin"

# Debian / Ubuntu
sudo apt install ffmpeg
```

ttcut looks for ffmpeg on `PATH`, then beside the script, then in the usual Windows install locations. Without it you can still tag and export JSON — you just can't render.

## Quick start

```bash
python3 ttcut_v2_3.py
```

That starts a local server on `127.0.0.1` and opens your browser. Then:

1. **Load video** for one recording, or **Load multiple videos…** for consecutive files from the same match. Multi-file selection starts in natural filename order; the file list shows each duration, global start/end, and total length. Use ↑/↓ before tagging to correct the order.
2. Type the two player names and pick who serves first.
3. Play, and tag: `S` the instant the ball leaves the bat on a serve, `A` or `B` when the point is won. Press `H` if that completed rally belongs in the highlights; press it again to remove the flag.
4. Tick **Stats board** if you want the summary card at the end.
5. **Render video.** Standard quality is recommended. The CPU based maximum quality option is much slower.
6. Optionally add a three second title card with tournament, category, and separate player/school fields, plus a matching 1280×720 YouTube thumbnail. Player labels such as `Name (School)` can populate the intro fields with the autofill button.
7. Choose **Output organization**. New matches default to **One folder per match**; **Same folder** retains the previous layout. The suggested name uses all six structured intro fields, `<tournament>_<category>_<player A>(<school A>)VS<player B>(<school B>).mp4`, even when the intro is disabled; otherwise it falls back to `<source>.cut.mp4`. **Save As…** chooses the destination root and basename. Its choice stays fixed as you edit the intro.

With **One folder per match**, output goes to `<root>/<basename>/<basename>.mp4` and `<root>/<basename>/<basename>.thumbnail.jpg`; tags go to `<root>/<basename>/ttcut-data/<basename>.tags.json`. Automatic output uses `_2`, `_3`, and so on when that match folder already exists. **Same folder** keeps the MP4, optional thumbnail, and tags together beside the source or in the Save As destination; automatic output suffixes an existing MP4. The output preview lists all three planned paths. Source recordings stay in their original locations. **New match** clears source segments, markers, score, and custom output path while keeping the organization preference; **Exit ttcut** stops the local server. Ctrl-C remains available.

### Tournament Highlight Builder

Choose **建立賽事精彩集錦…** and select a tournament root. The independent Builder recursively discovers modern `*/ttcut-data/*.tags.json` and legacy match-folder `*.tags.json`, collects `highlight: true` point events, and lets you uncheck, preview, and reorder highlights and matches without changing any source tags. It renders one MP4 from the original source videos: a three-second overall title, a one-second card for each match, then the selected rallies with the Koko scoreboard at the score state for that point. It also writes a 1280×720 thumbnail and a compact `.highlights.json` project manifest. Mixed media is normalized to 1080p/30 with aspect ratio preserved; common source resolution and frame rate are retained when all sources agree.

For multiple files, ttcut shows and saves one continuous clock. Candidate and event times, seeking, and cuts use this global time. Different resolution, frame rate, codec, or audio specs trigger a warning; rendering adjusts segments in one FFmpeg run and leaves no merged source file. If resolution differs, select an ROI for each segment before rally analysis. If the camera framing changes at the same resolution, enable the per-segment ROI option. The tags JSON stores source paths and offsets; reopening reports a missing segment by filename.

### Video scoreboard style

The **Scoreboard style** selector offers **Koko color table** (the default for new tags) and the original **ttcut** board. It affects only the rendered video. The choice is saved as `scoreboard.style` in the tags JSON; older tags without this field continue to use the original board. Koko uses two fixed rows in the lower left: a wide dark name cell, a blue `#19559B` games cell, and a green `#14703F` points cell. Names shrink within their cell when needed; numeric cells keep their size. The video board has no server marker. For command-line renders, `--scoreboard-style koko|ttcut` overrides the JSON choice.

### Keyboard

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `space` | play / pause | `←` `→` | step one frame |
| `S` | serve | `⇧←` `⇧→` | step 1 second |
| `A` | point to A | `⌥←` `⌥→` | step 5 seconds |
| `B` | point to B | `1` `2` `3` `4` | 0.5× / 1× / 1.5× / 2× |
| `H` | toggle selected/latest completed rally highlight | `Z` | undo the latest event |
| `N` | new game |  |  |

Clicking a historical point row seeks to it and selects it with an orange accent; `H` then toggles that selected rally. With no selected historical point, `H` still targets the latest rally that has both a serve and a point. Starting a new candidate/serve/point clears the historical selection, and an open serve must be scored first. Point rows also have a bordered star control that toggles without seeking; highlighted rows show `★ 精彩球`, and the header shows the live count. `×` deletes an event. The flag lives on the point event itself, for example `{"t": 25.0, "type": "point", "winner": "B", "highlight": true}`. Selection is UI-only and is never written to JSON; older JSON without the highlight field loads with zero highlights.

## Match format

| Setting | What it's for |
| --- | --- |
| Game to *n* points | 11 by default; set 21 for the old scoring |
| Standard / Capped | Standard is win-by-two. Capped ends the game when someone reaches the cap after 10:10 — useful for club rules that avoid endless deuces |
| Start games | Continuing a match that's split across several video files |
| Handicap | Starting points, applied every game or first game only |

Serve rotation is derived, not tagged: two serves each, one each at deuce, and the other player starts each new game. Handicap points don't shift the rotation, because they weren't played.

## The stats board

Tick **Stats board** and the last frame is held (1 second by default, adjustable) with a summary drawn over it. The corner scoreboard steps aside for it, and the audio is padded with silence so picture and sound stay the same length.

**Top half** — the per-game table, in the order broadcasts use: name, games won, then each game's score. The winner of each game is set bright and the loser dimmed, and the player leading on games gets the accent colour.

**Bottom half** — a side-by-side comparison: A's figure on the left, B's on the right, the metric name between them. Whichever side is ahead on a row is picked out in the accent colour.

| Metric | Definition |
| --- | --- |
| **Total points** | Rallies actually won. Handicap starting points are shown on the scoreboard but are not counted here — nobody won them. |
| **Service win rate** | Points won on own serve ÷ points served, with the raw fraction beside the label. This is the "how reliable are you when you start the rally" number. |
| **Longest point streak** | Longest run of consecutive points, **carried across games**. Two 11:0 games back to back reads as 22, not 11. |

The same numbers appear live in the sidebar as you tag, so you can sanity-check them before committing to a render.

A caveat worth knowing: service win rate is only as good as your serve tags. If you skip the `S` on some rallies, those points still count towards points won and streaks, but the rotation — and therefore the rate — will drift.

## Command line

For re-rendering without opening the UI:

```bash
python3 ttcut_v2_3.py match.tags.json match.MOV
python3 ttcut_v2_3.py match.tags.json match.MOV --quality max --stats --stats-hold 3
python3 ttcut_v2_3.py match.tags.json match.MOV --dry-run      # print the cut list, render nothing
```

| Flag | Default | Notes |
| --- | --- | --- |
| `-o`, `--out` | `<video>.cut.mp4` | output path |
| `--lead` / `--tail` | from JSON | seconds kept before a serve / after a point |
| `--min-cut` | `2.0` | shorter gaps are left alone rather than jump-cut |
| `--cut-lets` / `--let-tail` | off / `1.5` | also cut the retrieval between lets |
| `--stats` / `--no-stats` | from JSON | force the summary card on or off |
| `--stats-hold` | `1.0` | seconds to hold the card |
| `--accent` | `#FF7A18` | point digits and the bar beside the names |
| `--quality` | `high` | `fast`, `high`, `max` (libx264 CRF — much slower) |
| `--encoder` | platform default | `h264_videotoolbox` on Mac; `h264_nvenc`, `h264_qsv`, `h264_amf`, `libx264` on Windows |
| `--crf` / `--preset` / `--bitrate` | — | override the quality knobs directly |
| `--fps` | `source` | frame rate follows the source unless you give a number |
| `--hdr` | `auto` | `tonemap`, `keep` (needs HEVC), or `ignore` |
| `--size` | source | e.g. `1920x1080` |
| `--hwaccel` | `auto` | `videotoolbox` on Mac, else `none`, `cuda`, `qsv` |
| `--font` | platform default | font for the scoreboard names |
| `--ffmpeg` | — | path to `ffmpeg.exe` or its folder |
| `--port` / `--no-browser` | — | interface options |

Run `--help` for the full list.

## Tags file

The export is plain JSON, safe to hand-edit:

```jsonc
{
  "version": 2,
  "fps": 59.94,
  "players": { "A": "Player A", "B": "Player B" },
  "firstServer": "A",
  "format": { "pointsPerGame": 11, "deuce": "standard", "cap": 12 },
  "start": {
    "games":  { "A": 0, "B": 0 },
    "points": { "A": 0, "B": 0 },
    "handicapScope": "every"
  },
  "pads": { "tail": 1.0, "lead": 0.3 },
  "scoreboard": { "accent": "#FF7A18" },
  "stats": { "enabled": true, "hold": 1.0 },
  "events": [
    { "t": 12.35, "frame": 740, "type": "serve" },
    { "t": 18.90, "frame": 1133, "type": "point", "winner": "A" },
    { "t": 44.10, "frame": 2644, "type": "game" }
  ]
}
```

Files from older versions load fine; missing blocks fall back to defaults.

## How it works

- **Cutting** uses ffmpeg's `select` filter over a list of kept ranges rather than `trim`+`concat`, which buffers whole decoded segments in memory. Slower, but it doesn't fall over on long 4K files.
- **The scoreboard** is generated as an ASS subtitle track and burned in with the `subtitles` filter, so it scales cleanly to any resolution and costs nothing extra to redraw.
- **The freeze** is `tpad=stop_mode=clone` on the video and `apad` on the audio.
- **Scoring lives in one place.** The browser doesn't compute anything — it posts your events to the local Python process and renders what comes back. One implementation, so the preview and the burned-in board can't disagree.

## Which file do I download?

| File | Interface |
| --- | --- |
| `ttcut_v2_3.py` | Traditional Chinese |
| `ttcut_v2_3_EN.py` | English |

The two are verified to produce identical scoring, cut planning, and stats. Both keep CJK-capable fonts for the burned-in names, so a tags file with Chinese player names renders correctly in either build.

## Licence

MIT — see [LICENSE](LICENSE). Author: Mika ([@MikaDD-TW](https://github.com/MikaDD-TW)). Built iteratively with Claude, acknowledged voluntarily.
