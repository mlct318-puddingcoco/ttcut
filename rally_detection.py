#!/usr/bin/env python3
"""Rally Detection v3.0-A: coarse ROI motion with local dense refinement.

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


METHOD = "roi-motion-audio-local-rescue-v3.0-a"


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
    split_confidence_threshold: float = 0.78
    split_audio_penalty_per_hit: float = 0.045
    pre_roll_seconds: float = 0.70
    post_roll_seconds: float = 0.45
    audio_sample_rate: int = 8000
    audio_frame_ms: int = 10
    audio_highpass_hz: int = 900
    # v3.0-A keeps the 2 fps coarse path above. Denser decoding is restricted
    # to short, evidence-backed windows and candidate end boundaries.
    rescue_fps: float = 8.0
    rescue_audio_cluster_gap_seconds: float = 0.65
    rescue_min_audio_hits: int = 3
    rescue_max_audio_span_seconds: float = 2.6
    rescue_window_padding_seconds: float = 0.75
    rescue_coarse_motion_ratio: float = 0.62
    rescue_min_side_balance: float = 0.18
    rescue_min_visual_seconds: float = 0.45
    rescue_min_strong_seconds: float = 0.65
    rescue_long_peak_ratio: float = 1.60
    rescue_max_candidate_seconds: float = 3.6
    rescue_pre_roll_seconds: float = 0.35
    rescue_post_roll_seconds: float = 0.20
    rescue_overlap_guard_seconds: float = 0.35
    rescue_adjacent_guard_seconds: float = 1.2
    end_refine_fps: float = 8.0
    end_refine_lookback_seconds: float = 3.5
    end_refine_lookahead_seconds: float = 0.65
    end_refine_quiet_seconds: float = 0.50
    end_refine_audio_quiet_seconds: float = 0.45
    end_refine_min_shorten_seconds: float = 0.25
    end_refine_max_shorten_seconds: float = 2.0
    end_refine_post_roll_seconds: float = 0.12
    end_refine_windows_per_10_minutes: int = 4


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


def decode_motion(video_path, ffmpeg, roi, config, start=0.0, end=None,
                  sample_fps=None, keyframes_only=True):
    roi = validate_roi(roi)
    sample_fps = sample_fps or config.sample_fps
    crop = (
        f"crop=trunc(iw*{roi['w']}/2)*2:trunc(ih*{roi['h']}/2)*2:"
        f"trunc(iw*{roi['x']}/2)*2:trunc(ih*{roi['y']}/2)*2"
    )
    vf = (f"fps={sample_fps},{crop},"
          f"scale={config.analysis_width}:{config.analysis_height}:flags=area,format=gray")
    # Some DJI HEVC files emit one recoverable PPS warning per decoded keyframe;
    # fatal-only avoids filling stderr while stdout is streamed frame-by-frame.
    cmd = [ffmpeg, "-v", "fatal"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{max(0.0, start):.3f}",
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
        t = start + frame_index / sample_fps
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

    Audio never proposes or unconditionally vetoes a split. It only discounts
    confidence in a short pause; sustained visual evidence remains primary.
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
                     audioHits=hits, valleyDepth=None,
                     visualConfidence=None, audioPenalty=0.0,
                     splitConfidence=None, decision="keep", reason="")
        checks.append(check)
        if not before or not after:
            check["reason"] = "edge_valley"
            continue
        left_span = metrics[before[-1]]["t"] - metrics[before[0]]["t"] + frame_seconds
        right_span = metrics[after[-1]]["t"] - metrics[after[0]]["t"] + frame_seconds
        left_peak = max(smoothed[i] for i in before[-nearby:])
        right_peak = max(smoothed[i] for i in after[:nearby])
        check["leftPeak"] = round(left_peak, 2)
        check["rightPeak"] = round(right_peak, 2)
        flank_peak = min(left_peak, right_peak)
        depth = max(0.0, 1.0 - score / max(.001, flank_peak))
        visual_confidence = min(1.0, depth / .60) * .50
        visual_confidence += min(1.0, duration / 1.5) * .25
        visual_confidence += min(1.0, max(0.0, right_peak / threshold - 1) / .40) * .15
        visual_confidence += min(1.0, max(0.0, left_peak / threshold - 1) / .40) * .10
        # Hall sounds are spatially ambiguous: they can lower confidence but
        # cannot veto a long or strong visual valley outright.
        audio_penalty = (min(hits, 3) * config.split_audio_penalty_per_hit
                         * min(1.0, 1.5 / duration))
        split_confidence = max(0.0, visual_confidence - audio_penalty)
        check.update(valleyDepth=round(depth, 3),
                     visualConfidence=round(visual_confidence, 3),
                     audioPenalty=round(audio_penalty, 3),
                     splitConfidence=round(split_confidence, 3))
        if len(valley) < min_valley_frames:
            check["reason"] = "brief_valley"
            continue
        if min(left_span, right_span) < config.split_min_side_seconds:
            check["reason"] = "short_side"
            continue
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
        if split_confidence < config.split_confidence_threshold:
            check["reason"] = "low_split_confidence"
            continue
        viable.append((first, last, check))

    if not viable:
        return [(group, None)], checks or [dict(
            decision="keep", reason="no_sustained_valley")]

    # Evaluate every qualified valley in time order. A later, deeper valley
    # must not hide an earlier rally boundary; the fragment guard still limits
    # the final result to at most three plausible visual pieces.
    accepted = []
    for first, last, check in sorted(viable, key=lambda item: item[0]):
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
        base_confidence = min(0.99, 0.36 + 0.34 * visual_strength
                              + 0.18 * side_balance + 0.08 * active_ratio
                              + 0.04 * audio_support)
        candidate_duration = max(0.0, end - start)
        pre_motion = (sum(smoothed[max(0, first - 2):first]) /
                      max(1, len(smoothed[max(0, first - 2):first]))
                      if first else baseline)
        post_motion = (sum(smoothed[last + 1:last + 3]) /
                       max(1, len(smoothed[last + 1:last + 3]))
                       if last + 1 < len(smoothed) else baseline)
        rise = motion_peak - pre_motion
        fall = motion_peak - post_motion
        complete_pattern = (motion_peak >= threshold * 1.15
                            and rise >= threshold * .15
                            and fall >= threshold * .15)
        short_penalties = []
        if candidate_duration <= 4.0:
            if strong_frames < 3:
                short_penalties.append(("few_strong_frames", .10))
            if motion_peak < threshold * 1.20:
                short_penalties.append(("weak_visual_peak", .08))
            if not hits:
                short_penalties.append(("no_audio_support", .10))
            if candidate_duration < 2.75:
                short_penalties.append(("very_short", .04))
            if not complete_pattern:
                short_penalties.append(("incomplete_rise_fall", .04))
        short_penalty = sum(weight for _, weight in short_penalties)
        confidence = max(0.0, base_confidence - short_penalty)
        confidence_tier = ("low" if candidate_duration <= 4.0 and confidence < .68
                           else "normal")
        candidates.append(dict(
            start=round(start, 3), end=round(max(start, end), 3),
            duration=round(candidate_duration, 3),
            visualStart=round(metrics[first]["t"], 3),
            visualEnd=round(metrics[last]["t"], 3),
            motionMean=round(motion_mean, 2), motionPeak=round(motion_peak, 2),
            activeRatio=round(active_ratio, 2), sideBalance=round(side_balance, 2),
            strongFrames=strong_frames, supportFrames=len(group),
            audioHits=len(hits), audioSupport=round(audio_support, 2),
            confidence=round(confidence, 2),
            baseConfidence=round(base_confidence, 2),
            confidenceTier=confidence_tier,
            shortEvidence=dict(rise=round(rise, 2), fall=round(fall, 2),
                               completePattern=complete_pattern,
                               peakThresholdRatio=round(motion_peak / threshold, 2),
                               penalty=round(short_penalty, 2),
                               penaltyReasons=[reason for reason, _ in short_penalties]),
            boundaryBasis=boundary_basis,
            splitPoint=(split_check.get("point")
                        if split_check["decision"] == "split" else None),
            motionValleyScore=split_check.get("motionValleyScore"),
            motionValleyDuration=split_check.get("motionValleyDuration"),
            valleyDepth=split_check.get("valleyDepth"),
            splitAudioHits=split_check.get("audioHits", 0),
            splitVisualConfidence=split_check.get("visualConfidence"),
            splitAudioPenalty=split_check.get("audioPenalty", 0.0),
            splitConfidence=split_check.get("splitConfidence"),
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


def _interval_overlap(first_start, first_end, second_start, second_end):
    return max(0.0, min(first_end, second_end) - max(first_start, second_start))


def _interval_gap(first_start, first_end, second_start, second_end):
    if _interval_overlap(first_start, first_end, second_start, second_end) > 0:
        return 0.0
    return max(first_start, second_start) - min(first_end, second_end)


def _audio_clusters(audio_impacts, max_gap):
    clusters = []
    for impact in sorted(audio_impacts):
        if not clusters or impact - clusters[-1][-1] > max_gap:
            clusters.append([impact])
        else:
            clusters[-1].append(impact)
    return clusters


def propose_rescue_windows(metrics, audio_impacts, candidates, config=None,
                           duration=None):
    """Return short local-rescan windows backed by audio and coarse motion.

    Audio is allowed to propose a window, but not a rally. A proposal must also
    contain bilateral coarse ROI motion and must not overlap an existing coarse
    candidate. The dense pass applies the final visual gate.
    """
    config = config or DetectorConfig()
    if not metrics or not audio_impacts:
        return []
    raw = [item["motion"] for item in metrics]
    smoothed = _smooth(raw)
    baseline = percentile(smoothed, config.baseline_percentile)
    activity = percentile(smoothed, config.activity_percentile)
    threshold_delta = max(config.min_threshold,
                          (activity - baseline) * config.threshold_fraction)
    proposal_floor = baseline + threshold_delta * config.rescue_coarse_motion_ratio
    frame_seconds = 1 / config.sample_fps
    stop = duration if duration is not None else metrics[-1]["t"] + frame_seconds
    windows = []
    for cluster in _audio_clusters(
            audio_impacts, config.rescue_audio_cluster_gap_seconds):
        if len(cluster) < config.rescue_min_audio_hits:
            continue
        audio_span = cluster[-1] - cluster[0]
        if audio_span > config.rescue_max_audio_span_seconds:
            continue
        core_start = max(0.0, cluster[0] - .12)
        core_end = min(stop, cluster[-1] + .12)
        start = max(0.0, core_start - config.rescue_window_padding_seconds)
        end = min(stop, core_end + config.rescue_window_padding_seconds)
        if any(
            _interval_overlap(start, end, item["start"], item["end"])
            >= config.rescue_overlap_guard_seconds
            or _interval_gap(core_start, core_end, item["start"], item["end"])
            <= config.rescue_adjacent_guard_seconds
            for item in candidates
        ):
            continue
        nearby = [i for i, item in enumerate(metrics)
                  if start - frame_seconds <= item["t"] <= end + frame_seconds]
        elevated = [i for i in nearby if smoothed[i] >= proposal_floor]
        if not elevated:
            continue
        # Smoothing spreads one burst into adjacent quiet frames. Use the raw
        # peak for the bilateral check so an idle neighbour cannot make a
        # one-sided walk look balanced.
        strongest = max(elevated, key=lambda i: raw[i])
        side_balance = (min(metrics[strongest]["left"], metrics[strongest]["right"])
                        / max(.001, metrics[strongest]["left"],
                              metrics[strongest]["right"]))
        if side_balance < config.rescue_min_side_balance * .65:
            continue
        windows.append(dict(
            start=round(start, 3), end=round(end, 3),
            coreStart=round(core_start, 3), coreEnd=round(core_end, 3),
            audioHits=len(cluster), coarseMotionPeak=round(smoothed[strongest], 2),
            coarseMotionRatio=round(smoothed[strongest] / max(.001, proposal_floor), 2),
            coarseSideBalance=round(side_balance, 3),
        ))
    # Overlapping audio clusters describe one suspicious episode. Keep one
    # dense decode and retain the combined core/evidence.
    merged = []
    for window in windows:
        if not merged or window["start"] > merged[-1]["end"]:
            merged.append(window)
            continue
        previous = merged[-1]
        previous["end"] = max(previous["end"], window["end"])
        previous["coreStart"] = min(previous["coreStart"], window["coreStart"])
        previous["coreEnd"] = max(previous["coreEnd"], window["coreEnd"])
        previous["audioHits"] += window["audioHits"]
        if window["coarseMotionPeak"] > previous["coarseMotionPeak"]:
            previous["coarseMotionPeak"] = window["coarseMotionPeak"]
            previous["coarseMotionRatio"] = window["coarseMotionRatio"]
            previous["coarseSideBalance"] = window["coarseSideBalance"]
    return merged


def analyze_rescue_window(metrics, audio_impacts, proposal, config=None,
                          duration=None):
    """Validate one proposed rescue window using dense visual evidence."""
    config = config or DetectorConfig()
    if len(metrics) < 4:
        return None
    raw = [item["motion"] for item in metrics]
    smoothed = _smooth(raw)
    baseline = percentile(smoothed, .25)
    activity = percentile(smoothed, .88)
    delta = max(.28, (activity - baseline) * .42)
    threshold = baseline + delta
    support = baseline + max(.16, delta * .48)
    support_active = [i for i, value in enumerate(smoothed) if value >= support]
    max_gap = max(1, round(.28 * config.rescue_fps))
    groups = []
    for index in support_active:
        if not groups or index - groups[-1][-1] > max_gap:
            groups.append([index])
        else:
            groups[-1].append(index)
    best = None
    for group in groups:
        first, last = group[0], group[-1]
        visual_start = metrics[first]["t"]
        visual_end = metrics[last]["t"]
        visual_span = visual_end - visual_start + 1 / config.rescue_fps
        if visual_span < config.rescue_min_visual_seconds:
            continue
        if _interval_overlap(visual_start, visual_end + 1 / config.rescue_fps,
                             proposal["coreStart"], proposal["coreEnd"]) <= 0:
            continue
        strong = [i for i in group if smoothed[i] >= threshold]
        if len(strong) < max(2, math.ceil(
                config.rescue_min_strong_seconds * config.rescue_fps)):
            continue
        window = metrics[first:last + 1]
        left_mean = sum(item["left"] for item in window) / len(window)
        right_mean = sum(item["right"] for item in window) / len(window)
        side_balance = min(left_mean, right_mean) / max(.001, left_mean, right_mean)
        if side_balance < config.rescue_min_side_balance:
            continue
        start = max(0.0, visual_start - config.rescue_pre_roll_seconds)
        stop = duration if duration is not None else metrics[-1]["t"] + 1 / config.rescue_fps
        end = min(stop, visual_end + config.rescue_post_roll_seconds)
        if end - start > config.rescue_max_candidate_seconds:
            continue
        hits = [t for t in audio_impacts if start - .08 <= t <= end + .08]
        if len(hits) < config.rescue_min_audio_hits:
            continue
        pre = smoothed[max(0, first - 4):first]
        post = smoothed[last + 1:min(len(smoothed), last + 5)]
        motion_peak = max(smoothed[first:last + 1])
        peak_threshold_ratio = motion_peak / max(.001, threshold)
        # v3.0-A targets brief rallies. A rescue that grows beyond two seconds
        # needs a distinctly stronger local visual peak; otherwise it is more
        # likely to be between-rally walking/reset activity.
        if end - start > 2.0 and peak_threshold_ratio < config.rescue_long_peak_ratio:
            continue
        rise = motion_peak - (sum(pre) / len(pre) if pre else baseline)
        fall = motion_peak - (sum(post) / len(post) if post else baseline)
        # A complete local burst is the main guard against nearby walking or
        # continuous background-table motion.
        if rise < delta * .20 or fall < delta * .20:
            continue
        motion_mean = sum(smoothed[first:last + 1]) / (last - first + 1)
        active_ratio = len(group) / max(1, last - first + 1)
        visual_strength = min(1.0, max(0.0, (motion_mean - baseline)
                                      / max(.001, activity - baseline)))
        audio_support = min(1.0, len(hits) / max(2.0, visual_span * 1.8))
        confidence = min(.99, .36 + .34 * visual_strength
                         + .18 * side_balance + .08 * active_ratio
                         + .04 * audio_support)
        candidate = dict(
            start=round(start, 3), end=round(max(start, end), 3),
            duration=round(max(0.0, end - start), 3),
            visualStart=round(visual_start, 3), visualEnd=round(visual_end, 3),
            motionMean=round(motion_mean, 2), motionPeak=round(motion_peak, 2),
            activeRatio=round(active_ratio, 2), sideBalance=round(side_balance, 2),
            strongFrames=len(strong), supportFrames=len(group),
            audioHits=len(hits), audioSupport=round(audio_support, 2),
            confidence=round(confidence, 2), baseConfidence=round(confidence, 2),
            confidenceTier="low" if confidence < .68 else "normal",
            shortEvidence=dict(
                rise=round(rise, 2), fall=round(fall, 2), completePattern=True,
                peakThresholdRatio=round(peak_threshold_ratio, 2),
                penalty=0.0, penaltyReasons=[]),
            boundaryBasis="local-motion+audio-rescue",
            splitPoint=None, motionValleyScore=None, motionValleyDuration=None,
            valleyDepth=None, splitAudioHits=0, splitVisualConfidence=None,
            splitAudioPenalty=0.0, splitConfidence=None,
            splitDecision="keep", splitReason="short_rescue", splitChecks=[],
            rescueApplied=True,
            rescueBasis="audio-cluster+bilateral-coarse-motion+dense-motion",
            rescueWindow=[proposal["start"], proposal["end"]],
            rescueAudioHits=len(hits), rescueSampleFps=config.rescue_fps,
            rescueCoarseMotionPeak=proposal.get("coarseMotionPeak"),
            rescueCoarseMotionRatio=proposal.get("coarseMotionRatio"),
            rescueCoarseSideBalance=proposal.get("coarseSideBalance"),
            refinedEnd=False, originalEnd=round(end, 3), endRefineBasis=None,
        )
        score = (len(hits), len(strong), side_balance, motion_peak)
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def refine_candidate_end(candidate, metrics, audio_impacts, config=None):
    """Shorten an over-extended end at a dense visual/audio quiet transition.

    Start is copied verbatim. Refinement is deliberately one-way in v3.0-A:
    the existing coarse post-roll already protects against early cuts, while an
    extension would be much more likely to absorb unrelated background motion.
    """
    config = config or DetectorConfig()
    result = dict(candidate)
    original_end = float(candidate["end"])
    result.setdefault("rescueApplied", False)
    result.setdefault("rescueBasis", None)
    result.setdefault("rescueWindow", None)
    result.setdefault("rescueAudioHits", 0)
    result.setdefault("rescueSampleFps", None)
    result.update(refinedEnd=False, originalEnd=round(original_end, 3),
                  endRefineBasis=None)
    if len(metrics) < 6:
        return result
    raw = [item["motion"] for item in metrics]
    smoothed = _smooth(raw)
    baseline = percentile(smoothed, .30)
    activity = percentile(smoothed, .82)
    delta = max(.24, (activity - baseline) * .38)
    active_threshold = baseline + delta
    quiet_threshold = baseline + delta * .58
    quiet_frames = max(3, math.ceil(config.end_refine_quiet_seconds
                                     * config.end_refine_fps))
    before_frames = max(4, math.ceil(.75 * config.end_refine_fps))
    for index in range(before_frames, len(metrics) - quiet_frames + 1):
        point = metrics[index]["t"]
        proposed_end = point + config.end_refine_post_roll_seconds
        shortening = original_end - proposed_end
        if shortening < config.end_refine_min_shorten_seconds:
            continue
        if shortening > config.end_refine_max_shorten_seconds:
            continue
        if proposed_end <= candidate["start"] + .45:
            continue
        before_indices = range(max(0, index - before_frames), index)
        after_indices = range(index, min(len(metrics), index + quiet_frames))
        if sum(smoothed[i] >= active_threshold for i in before_indices) < 2:
            continue
        if sum(smoothed[i] <= quiet_threshold for i in after_indices) < quiet_frames - 1:
            continue
        active_rows = [metrics[i] for i in before_indices
                       if smoothed[i] >= active_threshold]
        left = sum(item["left"] for item in active_rows) / len(active_rows)
        right = sum(item["right"] for item in active_rows) / len(active_rows)
        side_balance = min(left, right) / max(.001, left, right)
        if side_balance < config.rescue_min_side_balance:
            continue
        recent_hits = [t for t in audio_impacts if point - 1.8 <= t <= point + .08]
        quiet_hits = [t for t in audio_impacts
                      if point + .08 < t <= point + config.end_refine_audio_quiet_seconds]
        if len(recent_hits) < 2 or quiet_hits:
            continue
        result["end"] = round(proposed_end, 3)
        result["duration"] = round(max(0.0, proposed_end - result["start"]), 3)
        result["refinedEnd"] = True
        result["endRefineBasis"] = "dense-motion-fall+audio-quiet"
        result["endRefinePoint"] = round(point, 3)
        result["endRefineDelta"] = round(proposed_end - original_end, 3)
        result["endRefineSampleFps"] = config.end_refine_fps
        return result
    return result


def _candidate_overlaps(candidate, others, minimum):
    return any(_interval_overlap(candidate["start"], candidate["end"],
                                 item["start"], item["end"]) >= minimum
               for item in others)


def _end_refine_priority(candidate, audio_impacts, config):
    """Rank ends that already look over-extended in coarse/audio evidence."""
    end = candidate["end"]
    start = max(candidate["start"], end - config.end_refine_lookback_seconds)
    hits = [t for t in audio_impacts if start <= t <= end + .08]
    if len(hits) < 2:
        return None
    last_hit_gap = end - hits[-1]
    if last_hit_gap < config.end_refine_min_shorten_seconds + .12:
        return None
    recent_cluster = [t for t in hits if hits[-1] - 1.8 <= t <= hits[-1]]
    if len(recent_cluster) < 2:
        return None
    duration = candidate["end"] - candidate["start"]
    # Long quiet tails are the strongest signal; duration is only a tiebreaker.
    return last_hit_gap * 10 + min(duration, 12) / 12


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
    stop = stop if stop is not None else analyzed_end

    rescue_windows = propose_rescue_windows(
        metrics, audio_impacts, candidates, config, stop)
    rescued = []
    local_decode_failures = []
    rescue_decoded_seconds = 0.0
    for window in rescue_windows:
        try:
            dense = decode_motion(
                video_path, ffmpeg_path, roi, config,
                window["start"], window["end"],
                sample_fps=config.rescue_fps, keyframes_only=False)
        except DetectionError as exc:
            local_decode_failures.append(dict(stage="rescue", window=window,
                                              error=str(exc)))
            continue
        rescue_decoded_seconds += window["end"] - window["start"]
        candidate = analyze_rescue_window(
            dense, audio_impacts, window, config, stop)
        if candidate is None:
            continue
        if _candidate_overlaps(candidate, candidates + rescued,
                               config.rescue_overlap_guard_seconds):
            continue
        rescued.append(candidate)
    candidates.extend(rescued)
    candidates.sort(key=lambda item: (item["start"], item["end"]))

    priorities = []
    for index, candidate in enumerate(candidates):
        priority = _end_refine_priority(candidate, audio_impacts, config)
        if priority is not None:
            priorities.append((priority, index))
    video_minutes = max(1.0, (stop - start) / 600.0)
    end_window_budget = max(1, math.ceil(
        config.end_refine_windows_per_10_minutes * video_minutes))
    selected_end_indices = {index for _, index in sorted(
        priorities, reverse=True)[:end_window_budget]}

    refined = []
    refined_count = 0
    end_decoded_seconds = 0.0
    for candidate_index, candidate in enumerate(candidates):
        if candidate_index not in selected_end_indices:
            refined.append(refine_candidate_end(candidate, [], [], config))
            continue
        window_start = max(start, candidate["end"]
                           - config.end_refine_lookback_seconds)
        window_end = min(stop, candidate["end"]
                         + config.end_refine_lookahead_seconds)
        if window_end - window_start < .75:
            refined.append(refine_candidate_end(candidate, [], [], config))
            continue
        try:
            dense = decode_motion(
                video_path, ffmpeg_path, roi, config, window_start, window_end,
                sample_fps=config.end_refine_fps, keyframes_only=False)
        except DetectionError as exc:
            local_decode_failures.append(dict(
                stage="end_refine", window=[round(window_start, 3),
                                             round(window_end, 3)],
                error=str(exc)))
            refined.append(refine_candidate_end(candidate, [], [], config))
            continue
        end_decoded_seconds += window_end - window_start
        item = refine_candidate_end(candidate, dense, audio_impacts, config)
        refined_count += bool(item["refinedEnd"])
        refined.append(item)
    candidates = refined
    return dict(
        version=3, method=METHOD, source=os.path.basename(video_path), roi=roi,
        candidates=candidates,
        diagnostics=dict(
            analyzedFrom=round(start, 3), analyzedTo=round(analyzed_end, 3),
            sampleFps=config.sample_fps,
            rescueSampleFps=config.rescue_fps,
            endRefineSampleFps=config.end_refine_fps,
            analysisSize=[config.analysis_width, config.analysis_height],
            **motion_diagnostics, audio=audio_diagnostics,
            rescueWindows=len(rescue_windows), rescuedCandidates=len(rescued),
            refinedEndCandidates=refined_count,
            endRefineEligibleCandidates=len(priorities),
            endRefineWindows=len(selected_end_indices),
            rescueDecodedSeconds=round(rescue_decoded_seconds, 3),
            endRefineDecodedSeconds=round(end_decoded_seconds, 3),
            localDecodeFailures=local_decode_failures,
            note=("主要候選仍由 2 fps ROI 視覺動態產生；只有音訊群集加上雙側粗略動作"
                  "支持的短視窗才進行局部高 fps rescue。候選終點可由局部高 fps 動作下降"
                  "及音訊安靜共同縮短；既有候選起點不變。"),
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rally Detection v3.0-A：2 fps ROI 粗掃與局部高 fps 修正。")
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
    print(f"Rally Detection v3.0-A · {result['source']}")
    print("候選由 ROI 影像產生；音訊只作輔助。人工確認前不會建立事件。\n")
    for i, item in enumerate(result["candidates"], 1):
        print(f"{i:3d}  {item['start']:8.2f} → {item['end']:8.2f}  "
              f"duration {item['duration']:5.2f}  "
              f"motion {item['motionMean']:5.2f}/{item['motionPeak']:5.2f}  "
              f"左右 {item['sideBalance']:.2f}  audio {item['audioHits']:2d}  "
              f"frames {item['strongFrames']}/{item['supportFrames']}  "
              f"{item['boundaryBasis']}  score {item['confidence']:.0%}"
              f" ({item['confidenceTier']})")
        valley = (f"{item['motionValleyScore']:.2f}/"
                  f"{item['motionValleyDuration']:.2f}s"
                  if item["motionValleyScore"] is not None else "—")
        print(f"     split {item['splitPoint'] if item['splitPoint'] is not None else '—'}"
              f" · valley {valley} · audio {item['splitAudioHits']}"
              f" · split confidence {item['splitConfidence']}"
              f" (visual {item['splitVisualConfidence']}"
              f" - audio {item['splitAudioPenalty']})"
              f" · {item['splitDecision']}: {item['splitReason']}")
        for check in item["splitChecks"]:
            if check.get("point") is not None:
                print(f"       valley {check['point']:.2f} · "
                      f"{check['motionValleyScore']:.2f}/"
                      f"{check['motionValleyDuration']:.2f}s · "
                      f"depth {check['valleyDepth']} · "
                      f"audio {check['audioHits']} (-{check['audioPenalty']}) · "
                      f"split {check['splitConfidence']} · "
                      f"{check['decision']}: {check['reason']}")
        if item["shortEvidence"]["penalty"]:
            print(f"       short confidence {item['baseConfidence']:.0%} → "
                  f"{item['confidence']:.0%} · "
                  f"{','.join(item['shortEvidence']['penaltyReasons'])}")
    diagnostics = result["diagnostics"]
    print(f"\n共 {len(result['candidates'])} 個視覺候選。"
          f" motion 閾值 {diagnostics['motionThreshold']}/"
          f"{diagnostics['motionSupportThreshold']}（佐證）；"
          f"佐證保留 {diagnostics['audioPromotedCandidates']}，"
          f"前後修剪 {diagnostics['audioTrimmedCandidates']}，"
          f"短走動排除 {diagnostics['rejectedBriefCandidates']}，"
          f"motion valley 切分 {diagnostics['motionValleySplits']}，"
          f"局部 rescue {diagnostics['rescuedCandidates']}/"
          f"{diagnostics['rescueWindows']}，"
          f"終點修正 {diagnostics['refinedEndCandidates']}。")


if __name__ == "__main__":
    main()
