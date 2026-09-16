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


METHOD = "roi-motion-audio-aux-v0.2.1"


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
    # Keep a second, lower visual threshold. A candidate still needs at least
    # one frame over the main threshold; audio may only promote the remaining
    # near-threshold visual frames, never create a range by itself.
    support_threshold_fraction: float = 0.40
    min_support_delta: float = 0.45
    auxiliary_audio_hits: int = 3
    max_inactive_gap_seconds: float = 0.75
    min_visual_seconds: float = 1.25
    min_active_frames: int = 3
    # Short, mostly one-sided movements with almost no matching impacts are
    # usually a player walking/resetting rather than a rally in the target clip.
    brief_visual_seconds: float = 2.75
    brief_min_side_balance: float = 0.18
    brief_min_audio_hits: int = 3
    # Secondary segmentation only; these do not change the ROI detector.
    split_min_group_seconds: float = 6.0
    split_min_side_seconds: float = 2.5
    split_min_valley_seconds: float = 1.0
    split_max_points_per_candidate: int = 2
    split_valley_threshold_ratio: float = 0.95
    split_valley_peak_ratio: float = 0.62
    split_restart_peak_ratio: float = 1.12
    split_audio_veto_hits: int = 2
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


def _trim_on_audio_gap(group, metrics, audio_impacts):
    """Trim a much shorter visual prelude/tail around a clear audio gap.

    This never returns two candidates. The audio hits must cover both ends of
    the visual group, and visual duration—not sound count—chooses which side to
    retain. Thus hall noise cannot seed a candidate or duplicate one.
    """
    if len(group) < 6:
        return group, "motion"
    visual_start, visual_end = metrics[group[0]]["t"], metrics[group[-1]]["t"]
    hits = [t for t in audio_impacts if visual_start - .25 <= t <= visual_end + .25]
    if len(hits) < 4:
        return group, "motion"
    if hits[0] - visual_start > 1.0 or visual_end - hits[-1] > 1.0:
        return group, "motion"
    choices = []
    for before, after in zip(hits, hits[1:]):
        gap = after - before
        if not 1.05 <= gap <= 2.2:
            continue
        midpoint = (before + after) / 2
        left = [index for index in group if metrics[index]["t"] < midpoint]
        right = [index for index in group if metrics[index]["t"] >= midpoint]
        if len(left) >= 3 and len(right) >= 3:
            choices.append((gap, left, right))
    if not choices:
        return group, "motion"
    _, left, right = max(choices, key=lambda item: item[0])
    original = group
    if len(left) >= len(right) * 1.5:
        kept, basis = left, "motion+audio-trim-tail"
    elif len(right) >= len(left) * 1.5:
        kept, basis = right, "motion+audio-trim-head"
    else:
        return original, "motion"

    def balance(indices):
        window = metrics[indices[0]:indices[-1] + 1]
        left_mean = sum(item["left"] for item in window) / len(window)
        right_mean = sum(item["right"] for item in window) / len(window)
        return min(left_mean, right_mean) / max(0.001, max(left_mean, right_mean))

    # A trim must not throw away the second player's visual evidence. This is
    # what protects a real longer rally whose impact rhythm happens to contain
    # a hall-noise gap.
    if balance(kept) < max(0.12, balance(original) * 0.75):
        return original, "motion"
    return kept, basis


def _split_on_motion_valleys(group, metrics, raw, smoothed, audio_impacts,
                             threshold, config):
    """Split a long visual group only at sustained valleys with visual restart.

    Audio cannot propose a split. Impacts during a shallow, short valley may
    veto one, protecting a single rally with a momentary motion pause.
    """
    frame_seconds = 1 / config.sample_fps
    span = (metrics[group[-1]]["t"] - metrics[group[0]]["t"]
            + frame_seconds)
    if span < config.split_min_group_seconds:
        return [(group, None)], [dict(decision="keep", reason="short_candidate")]

    low_limit = threshold * config.split_valley_threshold_ratio
    min_valley_frames = max(2, math.ceil(config.split_min_valley_seconds
                                          * config.sample_fps))
    nearby = max(2, round(2.5 * config.sample_fps))
    full = range(group[0], group[-1] + 1)
    runs = []
    run = []
    for index in full:
        if raw[index] <= low_limit:
            run.append(index)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)

    checks = []
    viable = []
    for valley in runs:
        if len(valley) < min_valley_frames:
            continue
        first, last = valley[0], valley[-1]
        before = [i for i in group if i < first]
        after = [i for i in group if i > last]
        point = (metrics[first]["t"] + metrics[last]["t"]) / 2
        score = sum(raw[i] for i in valley) / len(valley)
        duration = len(valley) * frame_seconds
        hits = sum(metrics[first]["t"] - .1 <= t <=
                   metrics[last]["t"] + frame_seconds + .1
                   for t in audio_impacts)
        check = dict(point=round(point, 3), motionValleyScore=round(score, 2),
                     motionValleyDuration=round(duration, 2),
                     audioHits=hits, decision="keep", reason="")
        checks.append(check)
        if not before or not after:
            check["reason"] = "edge_valley"
            continue
        left_span = metrics[before[-1]]["t"] - metrics[before[0]]["t"] + frame_seconds
        right_span = metrics[after[-1]]["t"] - metrics[after[0]]["t"] + frame_seconds
        if min(left_span, right_span) < config.split_min_side_seconds:
            check["reason"] = "short_side"
            continue
        left_peak = max(smoothed[i] for i in before[-nearby:])
        right_peak = max(smoothed[i] for i in after[:nearby])
        check["leftPeak"] = round(left_peak, 2)
        check["rightPeak"] = round(right_peak, 2)
        if score > min(left_peak, right_peak) * config.split_valley_peak_ratio:
            check["reason"] = "shallow_valley"
            continue
        if (right_peak < threshold * config.split_restart_peak_ratio
                or sum(smoothed[i] >= threshold for i in after[:nearby]) < 2):
            check["reason"] = "no_visual_restart"
            continue
        if (left_peak < threshold * config.split_restart_peak_ratio
                or sum(smoothed[i] >= threshold for i in before[-nearby:]) < 2):
            check["reason"] = "weak_visual_before"
            continue
        if (duration <= 1.5 and hits >= config.split_audio_veto_hits
                and score > min(left_peak, right_peak) * 0.45):
            check["reason"] = "audio_continues_during_pause"
            continue
        viable.append((first, last, check))

    if not viable:
        return [(group, None)], checks or [dict(
            decision="keep", reason="no_sustained_valley")]

    # Choose strongest valleys first. Each resulting piece must remain a
    # plausible visual range; this prevents a long rally becoming fragments.
    accepted = []
    for first, last, check in sorted(
            viable, key=lambda item: item[2]["motionValleyScore"]):
        if len(accepted) >= config.split_max_points_per_candidate:
            check["reason"] = "split_limit"
            continue
        proposed = sorted(accepted + [(first, last, check)])
        pieces = []
        cursor = group[0]
        for a, b, _ in proposed:
            pieces.append([i for i in group if cursor <= i < a])
            cursor = b + 1
        pieces.append([i for i in group if i >= cursor])
        def viable_piece(piece):
            if not piece or len(piece) < config.min_active_frames:
                return False
            if (metrics[piece[-1]]["t"] - metrics[piece[0]]["t"]
                    + frame_seconds < config.split_min_side_seconds):
                return False
            window = metrics[piece[0]:piece[-1] + 1]
            left = sum(item["left"] for item in window)
            right = sum(item["right"] for item in window)
            if min(left, right) / max(.001, max(left, right)) < .12:
                return False
            strong = sum(smoothed[i] >= threshold for i in piece)
            if strong >= config.min_active_frames:
                return True
            start = metrics[piece[0]]["t"] - config.pre_roll_seconds
            end = metrics[piece[-1]]["t"] + config.post_roll_seconds
            hits = sum(start <= t <= end for t in audio_impacts)
            return strong >= 1 and hits >= config.auxiliary_audio_hits

        if any(not viable_piece(piece) for piece in pieces):
            check["reason"] = "fragment_guard"
            continue
        accepted = proposed
        check["decision"] = "split"
        check["reason"] = "sustained_motion_valley_visual_restart"

    if not accepted:
        return [(group, None)], checks
    parts = []
    for index in range(len(accepted) + 1):
        start = accepted[index - 1][1] + 1 if index else group[0]
        end = accepted[index][0] if index < len(accepted) else group[-1] + 1
        part = [i for i in group if start <= i < end]
        point = accepted[index - 1][2] if index else accepted[0][2]
        parts.append((part, point))
    return parts, checks


def analyze_motion(metrics, audio_impacts=None, config=None, duration=None):
    """Create ROI visual candidates, then optionally split sustained valleys."""
    config = config or DetectorConfig()
    audio_impacts = audio_impacts or []
    if not metrics:
        return [], {"motionThreshold": None, "activeFrames": 0}

    raw = [item["motion"] for item in metrics]
    smoothed = _smooth(raw)
    baseline = percentile(smoothed, config.baseline_percentile)
    activity = percentile(smoothed, config.activity_percentile)
    threshold_delta = max(config.min_threshold,
                          (activity - baseline) * config.threshold_fraction)
    threshold = baseline + threshold_delta
    support_threshold = baseline + max(
        config.min_support_delta,
        threshold_delta * config.support_threshold_fraction,
    )
    strong_active = [i for i, score in enumerate(smoothed) if score >= threshold]
    strong_set = set(strong_active)
    support_active = [i for i, score in enumerate(smoothed)
                      if score >= support_threshold]
    max_gap_frames = max(1, round(config.max_inactive_gap_seconds * config.sample_fps))
    groups = []
    for index in support_active:
        if not groups or index - groups[-1][-1] > max_gap_frames:
            groups.append([index])
        else:
            groups[-1].append(index)

    segmented_groups = []
    all_split_checks = []
    for group in groups:
        parts, checks = _split_on_motion_valleys(
            group, metrics, raw, smoothed, audio_impacts, threshold, config)
        all_split_checks.extend(checks)
        if len(parts) > 1:
            for part, split_check in parts:
                segmented_groups.append((part, "motion-valley-split",
                                         split_check, checks))
        else:
            kept, basis = _trim_on_audio_gap(group, metrics, audio_impacts)
            best_check = max(checks, key=lambda check: (
                check["reason"] not in ("short_candidate", "edge_valley"),
                check.get("motionValleyDuration", 0),
                -check.get("motionValleyScore", float("inf")),
            ))
            segmented_groups.append((kept, basis, best_check, checks))
    candidates = []
    promoted = rejected_brief = 0
    trimmed = 0
    for group, boundary_basis, split_check, split_checks in segmented_groups:
        if "audio-trim" in boundary_basis:
            trimmed += 1
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
        strong_frames = sum(index in strong_set for index in group)
        if strong_frames < config.min_active_frames:
            # Audio is only a corroborating vote: the group must already be a
            # sustained visual range and contain a main-threshold visual seed.
            if strong_frames < 1 or len(hits) < config.auxiliary_audio_hits:
                continue
            boundary_basis = "motion+audio-support"
            promoted += 1
        if (visual_span <= config.brief_visual_seconds
                and side_balance < config.brief_min_side_balance
                and len(hits) < config.brief_min_audio_hits):
            rejected_brief += 1
            continue
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
            duration=round(max(0.0, end - start), 3),
            visualStart=round(metrics[first]["t"], 3),
            visualEnd=round(metrics[last]["t"], 3),
            motionMean=round(motion_mean, 2), motionPeak=round(motion_peak, 2),
            activeRatio=round(active_ratio, 2), sideBalance=round(side_balance, 2),
            strongFrames=strong_frames, supportFrames=len(group),
            audioHits=len(hits), audioSupport=round(audio_support, 2),
            confidence=round(confidence, 2), boundaryBasis=boundary_basis,
            splitPoint=(split_check.get("point")
                        if split_check["decision"] == "split" else None),
            motionValleyScore=split_check.get("motionValleyScore"),
            motionValleyDuration=split_check.get("motionValleyDuration"),
            splitAudioHits=split_check.get("audioHits", 0),
            splitDecision=split_check["decision"],
            splitReason=split_check["reason"], splitChecks=split_checks,
        ))
    diagnostics = dict(
        motionBaseline=round(baseline, 2), motionActivity=round(activity, 2),
        motionThreshold=round(threshold, 2),
        motionSupportThreshold=round(support_threshold, 2), frames=len(metrics),
        activeFrames=len(strong_active), supportFrames=len(support_active),
        visualGroups=len(groups), audioPromotedCandidates=promoted,
        rejectedBriefCandidates=rejected_brief, audioTrimmedCandidates=trimmed,
        audioAssistedSplits=0,
        motionValleySplits=sum(check["decision"] == "split"
                               for check in all_split_checks),
        motionValleysKept=sum(check["decision"] == "keep"
                             for check in all_split_checks),
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
            note=("候選由 ROI 視覺動態產生；長候選只在持續低動作及視覺重新啟動時切分。"
                  "音訊可否決短暫停頓的誤切，不能單獨建立回合。"),
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
    print(f"Rally Detection v0.2.1 · {result['source']}")
    print("候選由 ROI 影像產生；音訊只作輔助。人工確認前不會建立事件。\n")
    for i, item in enumerate(result["candidates"], 1):
        print(f"{i:3d}  {item['start']:8.2f} → {item['end']:8.2f}  "
              f"duration {item['duration']:5.2f}  "
              f"motion {item['motionMean']:5.2f}/{item['motionPeak']:5.2f}  "
              f"左右 {item['sideBalance']:.2f}  audio {item['audioHits']:2d}  "
              f"frames {item['strongFrames']}/{item['supportFrames']}  "
              f"{item['boundaryBasis']}  score {item['confidence']:.0%}")
        valley = (f"{item['motionValleyScore']:.2f}/"
                  f"{item['motionValleyDuration']:.2f}s"
                  if item["motionValleyScore"] is not None else "—")
        print(f"     split {item['splitPoint'] if item['splitPoint'] is not None else '—'}"
              f" · valley {valley} · audio {item['splitAudioHits']}"
              f" · {item['splitDecision']}: {item['splitReason']}")
    diagnostics = result["diagnostics"]
    print(f"\n共 {len(result['candidates'])} 個視覺候選。"
          f" motion 閾值 {diagnostics['motionThreshold']}/"
          f"{diagnostics['motionSupportThreshold']}（佐證）；"
          f"佐證保留 {diagnostics['audioPromotedCandidates']}，"
          f"前後修剪 {diagnostics['audioTrimmedCandidates']}，"
          f"短走動排除 {diagnostics['rejectedBriefCandidates']}，"
          f"motion valley 切分 {diagnostics['motionValleySplits']}。")


if __name__ == "__main__":
    main()
