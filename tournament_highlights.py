"""Tournament highlight discovery, planning, and rendering for Koko ttcut.

The builder deliberately stores point identities, never copied rally events.  Rally
boundaries and score states are always derived from the original match document.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from intro_card import font_directory, intro_ass, select_font, subtitle_filter
from match_io import make_sources


VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".avi", ".mkv")
PROJECT_TYPE = "koko-highlight-project"
PROJECT_VERSION = 1


class TournamentError(ValueError):
    pass


def _clean(value):
    return str(value or "").strip()


def split_player(value):
    """Return (name, school); support both full-width and ASCII parentheses."""
    value = _clean(value)
    match = re.match(r"^(.*?)\s*[（(]\s*([^）)]+)\s*[）)]\s*$", value)
    return ((match.group(1).strip(), match.group(2).strip()) if match
            else (value, ""))


def _is_match_doc(doc):
    return (isinstance(doc, dict) and isinstance(doc.get("events"), list)
            and isinstance(doc.get("players"), dict)
            and any(key in doc for key in ("source", "sources", "generator", "fps")))


def discover_tag_files(root):
    """Find modern ttcut-data tags and legacy direct-match tags recursively."""
    root = os.path.abspath(root)
    found = []
    for directory, dirs, files in os.walk(root):
        dirs.sort(key=str.casefold)
        files.sort(key=str.casefold)
        for name in files:
            if not name.casefold().endswith(".tags.json"):
                continue
            # Any .tags.json in ttcut-data is modern. Other locations are the
            # legacy direct placement; document validation filters unrelated JSON.
            found.append(os.path.join(directory, name))
    return found


def _match_folder(tags_path):
    parent = os.path.dirname(tags_path)
    return os.path.dirname(parent) if os.path.basename(parent).casefold() == "ttcut-data" else parent


def _resolve_source(path, tags_path, match_folder):
    if not path:
        return None
    path = os.path.expanduser(str(path))
    candidates = ([path] if os.path.isabs(path) else
                  [os.path.join(match_folder, path),
                   os.path.join(os.path.dirname(match_folder), path),
                   os.path.join(os.path.dirname(tags_path), path)])
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(candidates[0])


def source_paths(doc, tags_path):
    folder = _match_folder(tags_path)
    raw = []
    if isinstance(doc.get("sources"), list):
        raw = [item.get("path") for item in doc["sources"] if isinstance(item, dict)]
    if not raw and doc.get("source"):
        raw = [doc.get("source")]
    resolved = [_resolve_source(path, tags_path, folder) for path in raw if path]
    if resolved:
        return resolved

    stem = os.path.basename(tags_path)[:-len(".tags.json")]
    candidates = []
    try:
        candidates = [os.path.join(folder, name) for name in os.listdir(folder)
                      if os.path.splitext(name)[1].casefold() in VIDEO_EXTENSIONS
                      and os.path.splitext(name)[0] in
                      (stem, stem[:-4] if stem.endswith(".cut") else stem)]
    except OSError:
        pass
    return sorted(candidates, key=lambda p: os.path.basename(p).casefold())


def pair_highlights(events):
    """Pair each highlighted point with the last serve in its open rally."""
    ordered = sorted(enumerate(events), key=lambda item: (float(item[1].get("t", 0)), item[0]))
    open_serve = None
    pairs, invalid = [], []
    for original_index, event in ordered:
        kind = event.get("type")
        if kind == "serve":
            open_serve = event
        elif kind == "game":
            open_serve = None
        elif kind == "point":
            if event.get("highlight") is True:
                item = dict(point_index=original_index,
                            point_id=f"{original_index}:{float(event.get('t', 0)):.3f}",
                            point_time=float(event.get("t", 0)),
                            winner=event.get("winner"))
                if open_serve is None:
                    invalid.append(item)
                else:
                    pairs.append(dict(item, serve_time=float(open_serve["t"])))
            open_serve = None
    return pairs, invalid


def score_states_for_highlights(doc, pairs, fold_full, read_format):
    """Attach pre/post scores using the normal renderer's authoritative fold."""
    events = sorted(doc.get("events", []), key=lambda e: float(e.get("t", 0)))
    fmt, start = read_format(doc)
    first = 1 if doc.get("firstServer") == "B" else 0
    scoring = fold_full(events, fmt, start, first)
    states = scoring["states"]
    result = []
    for pair in pairs:
        t = pair["point_time"]
        before = states[0][1:]
        after = before
        for state in states[1:]:
            if state[0] < t:
                before = state[1:]
                after = before
            elif state[0] == t:
                after = state[1:]
            elif state[0] > t:
                break
        result.append(dict(pair, score_before=list(before), score_after=list(after)))
    return result


def _metadata(doc):
    intro = doc.get("intro") or {}
    players = doc.get("players") or {}
    a_name, a_school = split_player(players.get("A"))
    b_name, b_school = split_player(players.get("B"))
    return dict(tournament=_clean(intro.get("tournament")),
                playerA=_clean(intro.get("playerA")) or a_name,
                schoolA=_clean(intro.get("schoolA")) or a_school,
                playerB=_clean(intro.get("playerB")) or b_name,
                schoolB=_clean(intro.get("schoolB")) or b_school)


def derive_metadata(root, matches):
    values = [_clean(m["metadata"].get("tournament")) for m in matches]
    tournament = (values[0] if values and all(values) and len(set(values)) == 1 else
                  os.path.basename(root.rstrip(os.sep)) if not any(values) else "")
    identities = []
    for match in matches:
        md = match["metadata"]
        identities.append({(md["playerA"], md["schoolA"]), (md["playerB"], md["schoolB"])})
    common = set.intersection(*identities) if identities else set()
    common = {(name, school) for name, school in common if name}
    protagonist, school = next(iter(common)) if len(common) == 1 else ("", "")
    return dict(tournament=tournament, title="精彩好球",
                protagonist=protagonist, school=school)


def scan_tournament(root, fold_full, read_format):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise TournamentError(f"賽事資料夾不存在：{root}")
    matches, warnings = [], []
    for tags_path in discover_tag_files(root):
        rel = os.path.relpath(tags_path, root)
        try:
            with open(tags_path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as ex:
            warnings.append(dict(type="malformed-tags", tags=tags_path,
                                 message=f"{rel}：標記 JSON 無法讀取（{ex}）"))
            continue
        if not _is_match_doc(doc):
            continue
        pairs, invalid = pair_highlights(doc.get("events", []))
        pairs = score_states_for_highlights(doc, pairs, fold_full, read_format)
        md = _metadata(doc)
        paths = source_paths(doc, tags_path)
        label = f"{md['playerA'] or 'A'} vs {md['playerB'] or 'B'}"
        match_id = os.path.relpath(tags_path, root).replace(os.sep, "/")
        highlights = []
        for item in pairs:
            highlights.append(dict(item, selected=True,
                                   label=f"{md.get('player' + item['winner'], item['winner'] or '?')}得分"))
        for item in invalid:
            warnings.append(dict(type="missing-serve", match=label, tags=tags_path,
                                 point_time=item["point_time"],
                                 message=(f"{label} {format_time(item['point_time'])}："
                                          "找不到發球事件，略過此精彩球")))
        matches.append(dict(id=match_id, tags=tags_path,
                            folder=_match_folder(tags_path), label=label,
                            metadata=md,
                            scoreboard_names=[_clean((doc.get("players") or {}).get("A")) or "A",
                                              _clean((doc.get("players") or {}).get("B")) or "B"],
                            sources=paths, highlights=highlights,
                            highlight_count=len(highlights), invalid_highlights=len(invalid)))
    matches.sort(key=lambda m: m["id"].casefold())
    active = [m for m in matches if m["highlights"]]
    return dict(root=root, scanned_matches=len(matches),
                total_highlights=sum(len(m["highlights"]) for m in matches),
                matches=active, warnings=warnings,
                metadata=derive_metadata(root, matches))


def format_time(seconds):
    minutes, secs = divmod(max(0.0, float(seconds)), 60)
    return f"{int(minutes):02d}:{secs:04.1f}"


def apply_review(scan, review):
    """Return a render plan without mutating scan or source tag documents."""
    by_id = {m["id"]: m for m in scan.get("matches", [])}
    order = review.get("match_order") or list(by_id)
    selected = review.get("selected") or {}
    highlight_order = review.get("highlight_order") or {}
    matches = []
    for match_id in order:
        if match_id not in by_id:
            raise TournamentError(f"未知比賽：{match_id}")
        source = by_id[match_id]
        points = {h["point_id"]: h for h in source["highlights"]}
        ids = highlight_order.get(match_id) or [h["point_id"] for h in source["highlights"]]
        match_selected = selected.get(match_id, {})
        chosen = [dict(points[pid]) for pid in ids
                  if pid in points and match_selected.get(pid, points[pid].get("selected", True))]
        if chosen:
            matches.append(dict(source, highlights=chosen))
    return dict(scan, matches=matches,
                selected_highlights=sum(len(m["highlights"]) for m in matches))


def validate_plan(plan, probe_video, probe_audio, ffprobe="ffprobe"):
    if not plan.get("selected_highlights"):
        raise TournamentError("沒有勾選任何精彩球。")
    hydrated = []
    for match in plan["matches"]:
        if not match.get("sources"):
            raise TournamentError(f"{match['label']}：標記檔沒有可辨識的來源影片。")
        missing = [path for path in match["sources"] if not os.path.isfile(path)]
        if missing:
            raise TournamentError(f"{match['label']}：找不到來源影片 {missing[0]}")
        try:
            sources = make_sources(match["sources"], probe_video, probe_audio, ffprobe)
        except Exception as ex:
            raise TournamentError(f"{match['label']}：媒體無法讀取（{ex}）") from ex
        total = sources[-1]["end"]
        points = []
        for item in match["highlights"]:
            start = max(0.0, item["serve_time"] - .8)
            end = min(total, item["point_time"] + 2.0)
            if end <= start:
                raise TournamentError(f"{match['label']} {format_time(item['point_time'])}：精彩球區間無效。")
            points.append(dict(item, start=start, end=end, duration=end-start))
        hydrated.append(dict(match, source_info=sources, highlights=points))
    return dict(plan, matches=hydrated)


def output_stem(metadata, safe_filename_part):
    tournament = safe_filename_part(metadata.get("tournament")) or "賽事"
    protagonist = safe_filename_part(metadata.get("protagonist"))
    return f"{tournament}_{protagonist}精彩好球" if protagonist else f"{tournament}_精彩好球"


def output_layout(base_mp4):
    base_mp4 = os.path.abspath(base_mp4)
    if os.path.splitext(base_mp4)[1].casefold() != ".mp4":
        raise TournamentError("輸出檔名需以 .mp4 結尾。")
    root, filename = os.path.split(base_mp4)
    stem = os.path.splitext(filename)[0]
    folder = os.path.join(root, stem)
    return dict(folder=folder, out=os.path.join(folder, filename),
                thumbnail=os.path.join(folder, stem + ".thumbnail.jpg"),
                manifest=os.path.join(folder, "ttcut-data", stem + ".highlights.json"))


def choose_profile(plan):
    """Preserve a common source profile; otherwise normalize to 1080p/30."""
    sources = [source for match in plan["matches"] for source in match["source_info"]]
    def audio_signature(source):
        audio = source.get("audio") or {}
        return tuple((key, str(audio.get(key, ""))) for key in
                     ("codec_name", "sample_rate", "channels", "channel_layout"))
    signatures = {(source["w"], source["h"], source["fps_frac"],
                   audio_signature(source)) for source in sources}
    common = len(signatures) == 1
    first = sources[0]
    return dict(size=(first["w"], first["h"]) if common else (1920, 1080),
                fps=first["fps"] if common else 30.0, normalized=not common)


def manifest_for(plan, metadata, settings):
    return dict(type=PROJECT_TYPE, version=PROJECT_VERSION,
                matches=[dict(tags=m["tags"],
                              selected_points=[h["point_time"] for h in m["highlights"]],
                              selected_point_ids=[h["point_id"] for h in m["highlights"]])
                         for m in plan["matches"]],
                metadata=dict(metadata), output=dict(settings))


def _escape_concat(path):
    return str(path).replace("'", "'\\''")


def _encoder_args(quality, encoder, bitrate="20M"):
    if encoder == "libx264":
        return ["-c:v", encoder, "-preset", "veryfast" if quality == "fast" else "medium",
                "-crf", "21" if quality == "fast" else "18", "-pix_fmt", "yuv420p"]
    return ["-c:v", encoder, "-b:v", bitrate, "-maxrate", bitrate,
            "-bufsize", "40M", "-pix_fmt", "yuv420p"]


def _write_title_ass(path, metadata, duration, size, separator=False):
    if separator:
        data = dict(tournament="", category="", playerA=metadata.get("playerA", ""),
                    schoolA=metadata.get("schoolA", ""), playerB=metadata.get("playerB", ""),
                    schoolB=metadata.get("schoolB", ""))
    else:
        data = {"lines": [metadata.get("tournament", ""),
                           metadata.get("title", "精彩好球"),
                           metadata.get("protagonist", ""),
                           metadata.get("school", "")]}
    font = select_font("")
    Path(path).write_text(intro_ass(data, size[0], size[1], duration, font), encoding="utf-8")
    return font


def title_command(ffmpeg, out, ass_path, duration, size, fps, quality, encoder):
    font = select_font("")
    sub = subtitle_filter(os.path.basename(ass_path), font_directory(font))
    return [ffmpeg, "-y", "-f", "lavfi", "-i",
            f"color=c=#04121F:s={size[0]}x{size[1]}:r={fps}:d={duration}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={duration}",
            "-vf", sub, *_encoder_args(quality, encoder), "-c:a", "aac", "-b:a", "256k",
            "-shortest", "-movflags", "+faststart", out]


def _overlaps(sources, start, end):
    result = []
    for source in sources:
        left, right = max(start, source["offset"]), min(end, source["end"])
        if right - left > .001:
            result.append((source, left - source["offset"], right - left))
    return result


def highlight_command(ffmpeg, out, ass_path, item, sources, size, fps, quality, encoder):
    overlaps = _overlaps(sources, item["start"], item["end"])
    args, filters, labels = [ffmpeg, "-y"], [], []
    for index, (source, local_start, duration) in enumerate(overlaps):
        args += ["-ss", f"{local_start:.6f}", "-t", f"{duration:.6f}", "-i", source["path"]]
        filters.append(f"[{index}:v]scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
                       f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                       f"format=yuv420p,setpts=PTS-STARTPTS[v{index}]")
        if source.get("audio"):
            filters.append(f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:"
                           f"channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f},"
                           f"asetpts=PTS-STARTPTS[a{index}]")
        labels.append(f"[v{index}][a{index}]")
    if len(overlaps) > 1:
        filters.append("".join(labels) + f"concat=n={len(overlaps)}:v=1:a=1[basev][outa]")
    else:
        filters += ["[v0]null[basev]", "[a0]anull[outa]"]
    sub = subtitle_filter(os.path.basename(ass_path), font_directory(select_font("")))
    filters.append(f"[basev]{sub}[outv]")
    return [*args, "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "[outa]",
            *_encoder_args(quality, encoder), "-c:a", "aac", "-b:a", "256k",
            "-shortest", "-movflags", "+faststart", out]


def prepare_render(plan, metadata, settings, layout, ffmpeg, build_ass,
                   font_name, font_num, accent, encoder):
    """Create only temporary scripts/commands. Caller owns returned temp directory."""
    tmp = tempfile.TemporaryDirectory(prefix="ttcut-highlights-")
    work = Path(tmp.name)
    size = tuple(settings.get("size", (1920, 1080)))
    fps = float(settings.get("fps", 30))
    quality = settings.get("quality", "high")
    clips, commands = [], []

    overall = work / "000-overall.mp4"
    overall_ass = work / "000-overall.ass"
    _write_title_ass(overall_ass, metadata, 3.0, size)
    commands.append(title_command(ffmpeg, str(overall), str(overall_ass), 3.0,
                                  size, fps, quality, encoder))
    clips.append(overall)
    sequence = 1
    for match in plan["matches"]:
        card = work / f"{sequence:03d}-match.mp4"
        card_ass = work / f"{sequence:03d}-match.ass"
        _write_title_ass(card_ass, match["metadata"], 1.0, size, separator=True)
        commands.append(title_command(ffmpeg, str(card), str(card_ass), 1.0,
                                      size, fps, quality, encoder))
        clips.append(card)
        sequence += 1
        names = match.get("scoreboard_names") or [
            match["metadata"].get("playerA") or "A",
            match["metadata"].get("playerB") or "B"]
        for item in match["highlights"]:
            clip = work / f"{sequence:03d}-rally.mp4"
            ass = work / f"{sequence:03d}-score.ass"
            switch = max(0.0, min(item["duration"], item["point_time"] - item["start"]))
            states = [(None, *item["score_before"]), (switch, *item["score_after"])]
            ass.write_text(build_ass(states, lambda t: t, item["duration"], names,
                                     size[0], size[1], font_name, font_num, accent,
                                     scoreboard_style="koko"), encoding="utf-8")
            commands.append(highlight_command(ffmpeg, str(clip), str(ass), item,
                                              match["source_info"], size, fps,
                                              quality, encoder))
            clips.append(clip)
            sequence += 1
    concat = work / "concat.txt"
    concat.write_text("".join(f"file '{_escape_concat(path)}'\n" for path in clips), encoding="utf-8")
    commands.append([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                     "-c", "copy", "-movflags", "+faststart", layout["out"]])
    thumb_ass = work / "thumbnail.ass"
    _write_title_ass(thumb_ass, metadata, 1.0, (1280, 720))
    # JPEG cannot use a video encoder; keep the common intro composition only.
    thumb = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=#04121F:s=1280x720:d=1",
             "-vf", subtitle_filter(thumb_ass.name, font_directory(select_font(""))),
             "-frames:v", "1", "-q:v", "2", layout["thumbnail"]]
    commands.append(thumb)
    return tmp, commands, clips


def run_commands(job, commands, workdir, cleanup, manifest_path, manifest):
    """Run a prepared render, aggregate progress by command count, always clean temp."""
    try:
        count = len(commands)
        for index, command in enumerate(commands):
            if job.state == "cancelled":
                return
            job.message = f"製作賽事精彩集錦 {index + 1}/{count}…"
            job.proc = subprocess.Popen(command, cwd=workdir, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, errors="replace")
            stdout, stderr = job.proc.communicate()
            if job.proc.returncode:
                job.state = "error"
                job.message = f"精彩集錦步驟 {index + 1} 失敗"
                job.log = stderr.splitlines()[-12:]
                return
            job.pct = (index + 1) / count * 100
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        job.state, job.pct, job.message = "done", 100.0, "完成"
    except Exception as ex:
        job.state, job.message = "error", f"賽事精彩集錦失敗：{ex}"
    finally:
        if job.state != "done":
            for path in (job.out, job.thumbnail):
                try:
                    if path and os.path.isfile(path):
                        os.unlink(path)
                except OSError:
                    pass
        cleanup.cleanup()
