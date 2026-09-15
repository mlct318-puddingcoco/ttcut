#!/usr/bin/env python3
"""Rally Detection v0.2: ROI visual motion with auxiliary audio evidence.

The detector is intentionally conservative about side effects: it only returns
review candidates.  It never creates ttcut events and never decides a winner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from array import array
from dataclasses import dataclass


METHOD = "roi-motion-audio-aux-v0.2"


@dataclass(frozen=True)
class DetectorConfig:
    # The representative DJI source is 4K/60 HEVC. Reading keyframes at 2 fps
    # keeps this prototype practical while retaining enough rally movement.
    sample_fps: float = 2.0
    analysis_width: int = 160
    analysis_height: int = 96
    pixel_delta: int = 12
    baseline_percentile: float = 0.35
    activity_percentile: float = 0.88
    threshold_fraction: float = 0.34
    min_threshold: float = 1.0
    max_inactive_gap_seconds: float = 1.30
    min_visual_seconds: float = 1.25
    min_active_frames: int = 3
    pre_roll_seconds: float = 0.70
    post_roll_seconds: float = 0.45
    audio_sample_rate: int = 8000
    audio_frame_ms: int = 10
    audio_highpass_hz: int = 900


class DetectionError(RuntimeError):
    pass


def percentile(values, q):
    if not values:
        raise ValueError("percentile needs at least one value")
    ordered = sorted(values)
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def validate_roi(roi):
    """Validate and normalize an ROI expressed as fractions of the video frame."""
    try:
        clean = {key: float(roi[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        raise DetectionError("ROI 格式不完整，請重新框選。")
    if not all(math.isfinite(value) for value in clean.values()):
        raise DetectionError("ROI 含有無效數值，請重新框選。")
    if clean["x"] < 0 or clean["y"] < 0 or clean["w"] <= 0 or clean["h"] <= 0:
        raise DetectionError("ROI 必須位於影片內。")
    if clean["x"] + clean["w"] > 1.0001 or clean["y"] + clean["h"] > 1.0001:
        raise DetectionError("ROI 超出影片範圍，請重新框選。")
    if clean["w"] < 0.08 or clean["h"] < 0.08:
        raise DetectionError("ROI 太小；請包含球桌與兩位選手的主要活動範圍。")
    return {key: round(value, 6) for key, value in clean.items()}


def _read_exact(stream, size):
    chunks, left = [], size
    while left:
        part = stream.read(left)
        if not part:
            break
        chunks.append(part)
        left -= len(part)
    return b"".join(chunks)


def _motion_between(previous, current, width, pixel_delta):
    """Return whole/left/right mean absolute motion, suppressing sensor noise."""
    midpoint = width // 2
    total = left = right = 0
    left_n = right_n = 0
    for index, (before, after) in enumerate(zip(previous, current)):
        delta = abs(after - before)
        contribution = delta if delta >= pixel_delta else 0
        total += contribution
        if index % width < midpoint:
            left += contribution
            left_n += 1
        else:
            right += contribution
            right_n += 1
    n = max(1, len(current))
    return total / n, left / max(1, left_n), right / max(1, right_n)


def decode_motion(video_path, ffmpeg, roi, config, start=0.0, end=None):
    roi = validate_roi(roi)
    crop = (
        f"crop=trunc(iw*{roi['w']}/2)*2:trunc(ih*{roi['h']}/2)*2:"
        f"trunc(iw*{roi['x']}/2)*2:trunc(ih*{roi['y']}/2)*2"
    )
    vf = (f"fps={config.sample_fps},{crop},"
          f"scale={config.analysis_width}:{config.analysis_height}:flags=area,format=gray")
    # Some DJI HEVC files emit one recoverable PPS warning per decoded keyframe;
    # fatal-only avoids filling stderr while stdout is streamed frame-by-frame.
    cmd = [ffmpeg, "-v", "fatal", "-skip_frame", "nokey",
           "-ss", f"{max(0.0, start):.3f}",
           "-i", os.path.abspath(video_path)]
    if end is not None:
        cmd += ["-t", f"{max(0.0, end - start):.3f}"]
    cmd += ["-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_size = config.analysis_width * config.analysis_height
    previous, metrics, frame_index = None, [], 0
    while True:
        frame = _read_exact(proc.stdout, frame_size)
        if not frame:
            break
        if len(frame) != frame_size:
            proc.kill()
            raise DetectionError("影片影格解碼不完整。")
        t = start + frame_index / config.sample_fps
        if previous is not None:
            whole, left, right = _motion_between(
                previous, frame, config.analysis_width, config.pixel_delta)
            metrics.append(dict(t=t, motion=whole, left=left, right=right))
        previous = frame
        frame_index += 1
    stderr = proc.stderr.read().decode("utf-8", "replace")
    code = proc.wait()
    if code != 0:
        detail = stderr.strip().splitlines()
        raise DetectionError(detail[-1] if detail else "ffmpeg 無法解碼 ROI 影像")
    if not metrics:
        raise DetectionError("影片太短或沒有可分析的影格。")
    return metrics


def _pcm_metrics(pcm, sample_rate, frame_ms):
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    frame_samples = max(1, round(sample_rate * frame_ms / 1000))
    result = []
    for offset in range(0, len(samples) - frame_samples + 1, frame_samples):
        frame = samples[offset:offset + frame_samples]
        square_sum = sum(value * value for value in frame)
        peak = max(abs(value) for value in frame)
        rms = math.sqrt(square_sum / frame_samples) / 32768.0
        result.append((20 * math.log10(max(1 / 32768, rms)),
                       20 * math.log10(max(1 / 32768, peak / 32768))))
    return result


def _audio_impacts(pcm, config, start=0.0):
    metrics = _pcm_metrics(pcm, config.audio_sample_rate, config.audio_frame_ms)
    if not metrics:
        return [], {"noiseFloorDb": None, "thresholdDb": None}
    levels = [rms for rms, _ in metrics]
    noise = percentile(levels, 0.35)
    activity = percentile(levels, 0.90)
    threshold = noise + max(8.0, (activity - noise) * 0.55)
    radius = max(1, round(0.20 / (config.audio_frame_ms / 1000)))
    active = []
    for i, (rms, peak) in enumerate(metrics):
        near = levels[max(0, i - radius):i] + levels[i + 1:i + radius + 1]
        local = percentile(near, 0.5) if near else rms
        if rms >= threshold and rms - local >= 3.2 and peak - rms >= 3.0:
            active.append(i)
    merge = max(1, round(0.12 / (config.audio_frame_ms / 1000)))
    groups = []
    for index in active:
        if not groups or index - groups[-1][-1] > merge:
            groups.append([index])
        else:
            groups[-1].append(index)
    impacts = [round(start + (max(group, key=lambda i: metrics[i][0]) + 0.5)
                     * config.audio_frame_ms / 1000, 3) for group in groups]
    return impacts, {"noiseFloorDb": round(noise, 1), "thresholdDb": round(threshold, 1)}


def decode_audio(video_path, ffmpeg, config, start=0.0, end=None):
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, start):.3f}",
           "-i", os.path.abspath(video_path)]
    if end is not None:
        cmd += ["-t", f"{max(0.0, end - start):.3f}"]
    cmd += ["-vn", "-ac", "1", "-af", f"highpass=f={config.audio_highpass_hz}",
            "-ar", str(config.audio_sample_rate), "-f", "s16le", "-acodec", "pcm_s16le",
            "pipe:1"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not proc.stdout:
        return [], {"available": False, "noiseFloorDb": None, "thresholdDb": None}
    impacts, diagnostics = _audio_impacts(proc.stdout, config, start)
    diagnostics.update(available=True, impacts=len(impacts))
    return impacts, diagnostics


def _smooth(values):
    if len(values) < 3:
        return list(values)
    result = []
    for i in range(len(values)):
        window = values[max(0, i - 1):min(len(values), i + 2)]
        result.append(sum(window) / len(window))
    return result


def _split_on_audio_gap(group, metrics, audio_impacts):
    """Use a clear sound gap only to split an already visual candidate.

    Audio cannot seed a range here. Both resulting ranges must retain at least
    two visually-active frames or the split is ignored.
    """
    if len(group) < 4:
        return [(group, "motion")]
    visual_start, visual_end = metrics[group[0]]["t"], metrics[group[-1]]["t"]
    if visual_end - visual_start < 2.5:
        return [(group, "motion")]
    hits = [t for t in audio_impacts if visual_start - .25 <= t <= visual_end + .25]
    choices = []
    for before, after in zip(hits, hits[1:]):
        gap = after - before
        midpoint = (before + after) / 2
        left_hits = sum(t <= before for t in hits)
        right_hits = sum(t >= after for t in hits)
        if 1.05 <= gap <= 2.2 and left_hits >= 2 and right_hits >= 2:
            left = [index for index in group if metrics[index]["t"] < midpoint]
            right = [index for index in group if metrics[index]["t"] >= midpoint]
            if len(left) >= 2 and len(right) >= 2:
                choices.append((gap, left, right))
    if not choices:
        return [(group, "motion")]
    _, left, right = max(choices, key=lambda item: item[0])
    return [(left, "motion+audio-gap"), (right, "motion+audio-gap")]


def analyze_motion(metrics, audio_impacts=None, config=None, duration=None):
    """Create candidates from visual motion; audio can only annotate/score them."""
    config = config or DetectorConfig()
    audio_impacts = audio_impacts or []
    if not metrics:
        return [], {"motionThreshold": None, "activeFrames": 0}

    raw = [item["motion"] for item in metrics]
    smoothed = _smooth(raw)
    baseline = percentile(smoothed, config.baseline_percentile)
    activity = percentile(smoothed, config.activity_percentile)
    threshold = baseline + max(config.min_threshold,
                               (activity - baseline) * config.threshold_fraction)
    active = [i for i, score in enumerate(smoothed) if score >= threshold]
    max_gap_frames = max(1, round(config.max_inactive_gap_seconds * config.sample_fps))
    groups = []
    for index in active:
        if not groups or index - groups[-1][-1] > max_gap_frames:
            groups.append([index])
        else:
            groups[-1].append(index)

    split_groups = []
    for group in groups:
        split_groups.extend(_split_on_audio_gap(group, metrics, audio_impacts))

    candidates = []
    for group, boundary_basis in split_groups:
        first, last = group[0], group[-1]
        visual_span = metrics[last]["t"] - metrics[first]["t"] + 1 / config.sample_fps
        if len(group) < config.min_active_frames or visual_span < config.min_visual_seconds:
            continue
        window = metrics[first:last + 1]
        left_mean = sum(item["left"] for item in window) / len(window)
        right_mean = sum(item["right"] for item in window) / len(window)
        side_balance = min(left_mean, right_mean) / max(0.001, max(left_mean, right_mean))
        # A rally should animate both players/sides at least somewhat. This remains
        # visual-only gating: audio never creates or extends a candidate.
        if side_balance < 0.12:
            continue
        start = max(0.0, metrics[first]["t"] - config.pre_roll_seconds)
        stop_limit = duration if duration is not None else metrics[-1]["t"] + 1 / config.sample_fps
        end = min(stop_limit, metrics[last]["t"] + config.post_roll_seconds)
        hits = [t for t in audio_impacts if start <= t <= end]
        motion_mean = sum(smoothed[i] for i in range(first, last + 1)) / (last - first + 1)
        motion_peak = max(smoothed[first:last + 1])
        active_ratio = len(group) / max(1, last - first + 1)
        visual_strength = min(1.0, max(0.0, (motion_mean - baseline) /
                                      max(0.001, activity - baseline)))
        audio_support = min(1.0, len(hits) / max(2.0, visual_span * 1.2))
        confidence = min(0.99, 0.36 + 0.34 * visual_strength
                         + 0.18 * side_balance + 0.08 * active_ratio
                         + 0.04 * audio_support)
        candidates.append(dict(
            start=round(start, 3), end=round(max(start, end), 3),
            visualStart=round(metrics[first]["t"], 3),
            visualEnd=round(metrics[last]["t"], 3),
            motionMean=round(motion_mean, 2), motionPeak=round(motion_peak, 2),
            activeRatio=round(active_ratio, 2), sideBalance=round(side_balance, 2),
            audioHits=len(hits), audioSupport=round(audio_support, 2),
            confidence=round(confidence, 2), boundaryBasis=boundary_basis,
        ))
    diagnostics = dict(
        motionBaseline=round(baseline, 2), motionActivity=round(activity, 2),
        motionThreshold=round(threshold, 2), frames=len(metrics),
        activeFrames=len(active), visualGroups=len(groups),
        audioAssistedSplits=max(0, len(split_groups) - len(groups)),
    )
    return candidates, diagnostics


def detect_video(video_path, roi, ffmpeg="ffmpeg", duration=None, start=0.0, end=None,
                 config=None):
    if not os.path.isfile(video_path):
        raise DetectionError(f"找不到影片：{video_path}")
    ffmpeg_path = shutil.which(ffmpeg) if not os.path.isabs(ffmpeg) else ffmpeg
    if not ffmpeg_path or not os.path.isfile(ffmpeg_path):
        raise DetectionError("找不到 ffmpeg")
    config = config or DetectorConfig()
    roi = validate_roi(roi)
    metrics = decode_motion(video_path, ffmpeg_path, roi, config, start, end)
    audio_impacts, audio_diagnostics = decode_audio(
        video_path, ffmpeg_path, config, start, end)
    stop = end if end is not None else duration
    candidates, motion_diagnostics = analyze_motion(
        metrics, audio_impacts, config, stop)
    analyzed_end = metrics[-1]["t"] + 1 / config.sample_fps
    return dict(
        version=2, method=METHOD, source=os.path.basename(video_path), roi=roi,
        candidates=candidates,
        diagnostics=dict(
            analyzedFrom=round(start, 3), analyzedTo=round(analyzed_end, 3),
            sampleFps=config.sample_fps,
            analysisSize=[config.analysis_width, config.analysis_height],
            **motion_diagnostics, audio=audio_diagnostics,
            note="候選由 ROI 視覺動態產生；音訊只參與既有候選的輔助分數。",
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rally Detection v0.2：ROI 視覺候選；音訊只作輔助，不判斷得分者。")
    parser.add_argument("video")
    parser.add_argument("--roi", required=True, help="x,y,w,h；皆為 0–1 比例")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = [float(value) for value in args.roi.split(",")]
        if len(values) != 4:
            raise ValueError
        roi = dict(zip(("x", "y", "w", "h"), values))
        result = detect_video(args.video, roi, args.ffmpeg, start=args.start, end=args.end)
    except (DetectionError, ValueError) as exc:
        parser.exit(2, f"偵測失敗：{exc}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"Rally Detection v0.2 · {result['source']}")
    print("候選由 ROI 影像產生；音訊只作輔助。人工確認前不會建立事件。\n")
    for i, item in enumerate(result["candidates"], 1):
        print(f"{i:3d}  {item['start']:8.2f} → {item['end']:8.2f}  "
              f"motion {item['motionMean']:5.2f}/{item['motionPeak']:5.2f}  "
              f"左右 {item['sideBalance']:.2f}  audio {item['audioHits']:2d}  "
              f"score {item['confidence']:.0%}")
    print(f"\n共 {len(result['candidates'])} 個視覺候選。")


if __name__ == "__main__":
    main()
