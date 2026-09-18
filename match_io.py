"""Multi-file source timeline and match output placement for ttcut."""

import os
import re


class SourceError(ValueError):
    pass


def natural_paths(paths):
    def key(path):
        return [int(piece) if piece.isdigit() else piece.casefold()
                for piece in re.split(r"(\d+)", os.path.basename(path))]
    return sorted(paths, key=key)


def make_sources(paths, probe_video, probe_audio, ffprobe="ffprobe"):
    if not paths:
        raise SourceError("請先選擇影片。")
    sources = []
    offset = 0.0
    for raw in paths:
        path = os.path.abspath(raw)
        if not os.path.isfile(path):
            raise SourceError(f"找不到 {os.path.basename(path)}")
        info = probe_video(path, ffprobe)
        if not info or not info.get("duration") or info["duration"] <= 0:
            raise SourceError(f"讀不到 {os.path.basename(path)} 的影片長度或規格。")
        duration = float(info["duration"])
        sources.append(dict(path=path, duration=duration, offset=offset,
                            end=offset + duration, w=info["w"], h=info["h"],
                            fps=info["fps"], fps_frac=info["fps_frac"],
                            codec=info["codec"], pix_fmt=info["pix_fmt"],
                            trc=info.get("trc", ""),
                            audio=probe_audio(path, ffprobe)))
        offset += duration
    return sources


def reorder_sources(sources, order):
    if sorted(order) != list(range(len(sources))):
        raise SourceError("影片順序無效。")
    result = []
    offset = 0.0
    for index in order:
        source = dict(sources[index])
        source["offset"] = offset
        source["end"] = offset + source["duration"]
        result.append(source)
        offset = source["end"]
    return result


def source_at(sources, t):
    """Exact joins belong to the next segment; the final endpoint stays final."""
    if not sources:
        raise SourceError("沒有影片來源。")
    t = max(0.0, min(float(t), sources[-1]["end"]))
    for index, source in enumerate(sources):
        if t < source["end"] or index == len(sources) - 1:
            return index, max(0.0, min(source["duration"], t - source["offset"]))
    raise AssertionError("unreachable")


def compatibility_issues(sources):
    if len(sources) < 2:
        return []
    first = sources[0]
    checks = (("w", "解析度"), ("h", "解析度"), ("fps_frac", "影格率"),
              ("codec", "影片編碼"), ("pix_fmt", "像素格式"), ("trc", "色彩規格"),
              ("audio", "音訊規格"))
    return sorted({label for source in sources[1:] for key, label in checks
                   if source.get(key) != first.get(key)})


def geometry_mismatch(sources):
    return any((s["w"], s["h"]) != (sources[0]["w"], sources[0]["h"])
               for s in sources[1:])


def output_layout(base_path, mode="same-folder", automatic=True):
    """Plan paths without creating a folder; auto outputs never reuse a target."""
    base_path = os.path.abspath(base_path)
    root = os.path.dirname(base_path)
    stem, ext = os.path.splitext(os.path.basename(base_path))
    if ext.lower() != ".mp4":
        raise ValueError("輸出檔名需以 .mp4 結尾。")
    if mode not in ("same-folder", "match-folder"):
        raise ValueError("未知輸出整理方式。")
    number = 1
    while True:
        name = stem if number == 1 else f"{stem}_{number}"
        folder = root if mode == "same-folder" else os.path.join(root, name)
        out = os.path.join(folder, name + ext)
        conflict = os.path.exists(out) if mode == "same-folder" else os.path.exists(folder)
        if not (automatic and conflict):
            break
        number += 1
    tags_folder = folder if mode == "same-folder" else os.path.join(folder, "ttcut-data")
    return dict(out=out, thumbnail=os.path.join(folder, name + ".thumbnail.jpg"),
                tags=os.path.join(tags_folder, name + ".tags.json"),
                folder=folder, conflict=conflict, mode=mode)
