# Koko ttcut

Turn raw table-tennis footage into a compact, scored match video with assisted rally detection, keyboard-first human review, highlights, and organized local output.

Koko ttcut is an enhanced fork of [MikaDD-TW/ttcut](https://github.com/MikaDD-TW/ttcut). It keeps the original project's practical manual tagging and FFmpeg rendering foundation, then adds a review workspace and a complete multi-match production workflow. The project remains available under the original [MIT License](LICENSE).

**English** · [繁體中文](README.zh-TW.md)

Koko ttcut runs locally. The application itself does not require an account or upload your source video to a cloud service.

## What Koko ttcut does

| Capability | Koko v1.4 workflow |
| --- | --- |
| **Assisted Rally Detection** | Finds candidates from visual activity inside a selected ROI, with audio used as supporting confidence evidence. Detection is a review aid, not an automatic scoring system. |
| **Rally Review Mode** | Opens candidates in chronological order for fast keyboard confirmation, scoring, skipping, and navigation. |
| **Manual Missed-Rally Recovery** | Press `S` at the real serve time, then `A` or `B`, without leaving Review Mode. |
| **Review Workspace** | Keeps an authoritative score HUD, next-serve indication (`應發球`), candidate progress, video, and event history together. |
| **Multi-file Match** | Treats 2–N consecutive clips as one continuous global match timeline. |
| **Scoreboard** | Renders the Koko color scoreboard from manually confirmed events; manual scoring remains authoritative. |
| **Highlight Marking** | Marks completed rallies during review or editing for later reuse. |
| **Tournament Highlight Builder** | Reviews and combines highlights from multiple matches into one compilation. |
| **Intro & Thumbnail** | Builds a three-second intro over natural source footage and automatically generates an optional matching YouTube thumbnail, with per-row single-line text fitting. |
| **Organized Output** | Writes the MP4, optional thumbnail, and `ttcut-data/*.tags.json` in a predictable match-folder layout. |

The original ttcut workflow centers on direct `S` / `A` / `B` manual tagging. Koko preserves that reliable path and adds assisted candidate detection, human review, multi-file handling, and tournament-level output. It does **not** claim fully automatic or perfect rally detection: the reviewer decides which candidates are real, and the human-entered score is the source of truth.

<!-- Add a verified Koko v1.4 Review Workspace screenshot here when one is available. -->

## Quick start

### Requirements

- **Python 3.8 or newer**
- **FFmpeg** with `libass` support (required by the `subtitles` filter used for scoreboards)

Install FFmpeg using the currently documented path for your platform:

```bash
# macOS
brew install ffmpeg

# Windows — download a build from https://www.gyan.dev/ffmpeg/builds/
# Put ffmpeg.exe beside the script, or pass --ffmpeg "C:\ffmpeg\bin"

# Debian / Ubuntu
sudo apt install ffmpeg
```

ttcut searches `PATH`, the script directory, and common Windows install locations. On Apple Silicon Macs it also prefers `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` when that installed build provides the required subtitle support. Without FFmpeg, you can still tag and export JSON, but you cannot render a finished video.

### Start the app

```bash
# Traditional Chinese interface
python3 ttcut_v2_3.py

# English interface
python3 ttcut_v2_3_EN.py
```

The script starts a local server on `127.0.0.1` and opens the browser interface.

1. Load one recording, or choose **Load multiple videos…** for consecutive clips from the same match.
2. Enter both player names, the first server, and the match format.
3. Either tag manually with `S`, `A`, and `B`, or select an ROI, run rally analysis, and enter **Rally Review Mode**.
4. Review the score and serve indication. Mark completed rallies with `H` when they belong in a highlight compilation.
5. Choose the Koko or original ttcut scoreboard, optional stats board, intro, thumbnail, quality, and output organization.
6. Render the match locally with FFmpeg.

## Core workflow: detection and Rally Review Mode

### Assisted candidate detection

Pause on a frame that clearly shows the target table and both players, draw an ROI around that activity area, and run rally analysis. The detector uses motion inside the ROI and supporting audio confidence to propose candidate intervals. Audio cannot create a rally by itself or decide who won the point.

Different source resolutions require an ROI for each segment. If the resolution is unchanged but the camera framing moves, enable per-segment ROI selection. Full analysis of 4K/60 fps HEVC footage may take several minutes.

Candidates may include false positives or miss real rallies. Low-confidence candidates stay visible for human judgment; selecting or previewing a candidate does not change match events.

### Rally Review Mode

Review Mode opens the first unreviewed candidate at a paused, segment-clamped 0.8-second pre-roll:

1. Press `Enter` to confirm the selected detector candidate as a serve. Auto-play is enabled by default.
2. Press `A` or `B` when the point ends. This updates the same authoritative event and scoring path used by normal editing.
3. Press `X` when the candidate is not a rally, or `[` / `]` to move between candidates.
4. If detection missed a rally, seek to the actual serve and press `S`, then score with `A` or `B`. This manual rally does not alter candidate progress.
5. Press `H` to toggle the relevant completed rally as a highlight.

The dedicated Review Workspace keeps the current game, games won, points, the player who should serve the next point (`🏓 應發球：player`), and candidate progress in a sticky HUD. Event history remains independently scrollable below the video. The HUD uses the same Python scoring result as the normal editor; it does not maintain a separate score or serve-rotation state.

Detector candidates and Review progress are session-local and are not written to the tags JSON. Rerunning detection, starting a new match, or restarting the app clears that review state. Confirmed match events and highlight flags remain in the normal tags file.

## Final match output

### Koko scoreboard and authoritative scoring

The **Scoreboard style** selector offers **Koko color table** for new tags and the original **ttcut** board for compatibility. Koko uses two fixed rows in the lower left: a dark name cell, blue `#19559B` games cell, and green `#14703F` points cell. Long names shrink inside the name cell; the rendered board intentionally has no server marker.

The scoreboard is derived from manually confirmed serve, point, and game events. Candidate confidence never changes the score. Serve rotation is derived from the first server and match rules: two serves each, one each at deuce, with the other player starting the next game.

### Intro and thumbnail

The optional normal-match intro runs for three seconds over the natural opening source video and audio; the scoreboard begins with the match itself. Tournament, category, player, and school/sublabel text shares one base size. Only a row or side that exceeds its width box shrinks, keeping each line intact. When enabled, the matching 1280×720 YouTube thumbnail is generated automatically with the same typography calculation.

### Stats board

The optional stats board holds the last frame and displays per-game scores plus total points, service win rate, and longest point streak. Service win rate depends on accurate serve events; missed or incorrect `S` events will make the derived rotation and rate inaccurate.

![The optional end-of-film stats board](stats-board.png)

## Highlights and Tournament Highlight Builder

After a completed rally, press `H` to toggle its highlight flag. You can also select a historical point row or use its star control. The flag is stored on the point event in the match tags JSON.

Choose **Tournament Highlight Builder** (`建立賽事精彩集錦…` in the Chinese interface) and select a tournament root. It recursively finds modern `*/ttcut-data/*.tags.json` and legacy match-folder `*.tags.json`, then collects valid `highlight: true` point events. You can preview, include/exclude, and reorder matches and rallies without modifying the source tags.

The Builder outputs one MP4 with:

- a three-second tournament intro over natural moving footage from the first selected rally;
- selected rallies in the chosen review order, without per-match separator cards;
- the Koko scoreboard at the authoritative score state for each rally;
- an optional 1280×720 thumbnail using the same natural background and typography; and
- a compact `ttcut-data/*.highlights.json` project manifest.

Common source resolution and frame rate are retained when all media agree. Mixed media is normalized to 1920×1080 at 30 fps with aspect ratio preserved.

## Multi-file matches and output organization

Multiple source files share one continuous global clock. Candidate times, match events, seeking, and cuts all use this timeline. ttcut warns about differences in resolution, frame rate, codec, or audio and adjusts segments in one FFmpeg render without creating a merged source file. Source recordings remain in their original locations.

With **One folder per match**, output uses this layout:

```text
<root>/
└── <basename>/
    ├── <basename>.mp4
    ├── <basename>.thumbnail.jpg       # when enabled
    └── ttcut-data/
        └── <basename>.tags.json
```

Automatic output appends `_2`, `_3`, and so on if a match folder already exists. **Same folder** retains the earlier flat layout. The output preview shows the planned video, thumbnail, and tags paths before rendering.

## Keyboard shortcuts

Shortcuts are ignored while typing in a field or using an interactive control.

| Key | Normal editor | Rally Review Mode |
| --- | --- | --- |
| `Space` | Play / pause | Play / pause |
| `←` / `→` | Step one frame | Step one frame |
| `Shift` + `←` / `→` | Seek 1 second | Seek 1 second |
| `Option` / `Alt` + `←` / `→` | Seek 5 seconds | Seek 5 seconds |
| `S` | Add a serve at the playhead | Add a manual missed-rally serve at the playhead, independent of the candidate |
| `Enter` | — | Confirm the current detector candidate as a serve |
| `A` / `B` | Award the point to A / B | Complete the open candidate-linked or manual rally |
| `X` | — | Skip the current candidate |
| `[` / `]` | — | Previous / next candidate |
| `H` | Toggle the selected or latest completed rally highlight | Same |
| `N` | New game | New game |
| `Z` | Undo the latest event | Undo the latest event |
| `1` / `2` / `3` / `4` | 0.5× / 1× / 1.5× / 2× playback | Same |

An open manual serve must be completed with `A` / `B` or removed with `Z` before `Enter` or `X` can continue candidate review. A candidate with a confirmed serve also cannot be skipped until that serve is undone.

## Match format and stats

| Setting | Purpose |
| --- | --- |
| Game to *n* points | 11 by default; set 21 for the old scoring system. |
| Standard / Capped | Standard is win-by-two. Capped ends the game at the configured cap after 10:10. |
| Start games | Continue a match whose earlier games were recorded separately. |
| Handicap | Apply starting points every game or in the first game only. |

Handicap points appear on the scoreboard but do not affect serve rotation or count as played points in the stats board.

## Command line rendering

For re-rendering an existing tags file without opening the UI:

```bash
python3 ttcut_v2_3.py match.tags.json match.MOV
python3 ttcut_v2_3.py match.tags.json match.MOV --quality max --stats --stats-hold 3
python3 ttcut_v2_3.py match.tags.json match.MOV --dry-run
```

Important options include `--out`, `--lead`, `--tail`, `--min-cut`, `--stats`, `--quality`, `--encoder`, `--fps`, `--hdr`, `--size`, `--hwaccel`, `--font`, `--ffmpeg`, and `--scoreboard-style koko|ttcut`. Run `python3 ttcut_v2_3.py --help` for the authoritative full list. The default minimum cut is 2.5 seconds.

## Tags and implementation notes

Tags are plain JSON and record sources, source offsets, players, match settings, scoreboard style, intro metadata, and timestamped events. Highlight state is stored on its point event, for example:

```json
{"t": 25.0, "type": "point", "winner": "B", "highlight": true}
```

Older tags files remain supported; missing blocks fall back to compatible defaults. Multi-file tags report missing source segments by filename when reopened.

Rendering uses FFmpeg. Kept ranges are selected without first creating a merged source file, scoreboards are generated as ASS subtitles and burned into the video, and scoring is computed by the local Python process so the editor HUD and rendered output share one implementation.

## Version and branches

- **Koko v1.4** is the current stable public workflow; detailed maintenance changes are recorded in [CHANGELOG.md](CHANGELOG.md).
- **`koko`** is the stable customized branch and the GitHub default branch.
- **`main`** is retained as the original/upstream baseline.

## Credits and license

Koko ttcut is a fork and extension of [MikaDD-TW/ttcut](https://github.com/MikaDD-TW/ttcut), originally authored by Mika ([@MikaDD-TW](https://github.com/MikaDD-TW)). Koko-specific workflow changes are maintained in this fork; this README does not imply that the upstream project was created here from scratch.

Licensed under the MIT License. See [LICENSE](LICENSE) for the copyright notice and full terms.
