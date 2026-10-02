#!/usr/bin/env python3
"""Rally Detection v3.0-C: calibrated review-evidence confidence.

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
import time
from array import array
from dataclasses import dataclass


METHOD = "roi-motion-audio-confidence-v3.0-c"


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
    rescue_audio_pair_max_gap_seconds: float = 0.95
    rescue_max_audio_span_seconds: float = 2.6
    rescue_window_padding_seconds: float = 0.75
    rescue_alt_post_padding_seconds: float = 1.25
    rescue_coarse_motion_ratio: float = 0.62
    rescue_min_side_balance: float = 0.18
    rescue_alt_min_audio_hits: int = 5
    rescue_alt_max_audio_span_seconds: float = 1.8
    rescue_alt_min_coarse_side_balance: float = 0.08
    rescue_min_visual_seconds: float = 0.45
    rescue_min_strong_seconds: float = 0.65
    rescue_long_peak_ratio: float = 1.60
    rescue_max_candidate_seconds: float = 3.6
    rescue_pre_roll_seconds: float = 0.35
    rescue_post_roll_seconds: float = 0.20
    rescue_overlap_guard_seconds: float = 0.35
    rescue_adjacent_guard_seconds: float = 1.2
    rescue_visual_gap_seconds: float = 0.20
    # A2's anti-merge exception is deliberately much narrower than the full
    # temporal-state work planned for v3.0-B. It only promotes a long-group
    # valley when it is sustained, audio-quiet, and followed by an unusually
    # strong visual restart.
    split_anti_merge_min_group_seconds: float = 12.0
    split_anti_merge_min_valley_seconds: float = 1.5
    split_anti_merge_valley_peak_ratio: float = 0.75
    split_anti_merge_restart_ratio: float = 3.0
    # v3.0-B keeps the existing coarse detector and places a small state model
    # around its evidence. A state transition needs persistence; one noisy
    # sample can neither enter a rally nor create a split.
    temporal_enter_persistence_seconds: float = 1.0
    temporal_leave_persistence_seconds: float = 1.0
    temporal_restart_persistence_seconds: float = 1.0
    temporal_min_group_seconds: float = 4.0
    temporal_min_child_seconds: float = 1.25
    temporal_min_valley_seconds: float = 0.5
    temporal_direct_valley_seconds: float = 1.5
    temporal_min_side_balance: float = 0.12
    temporal_continue_side_balance: float = 0.08
    temporal_direct_confidence: float = 0.82
    temporal_dense_confidence: float = 0.46
    temporal_dense_fps: float = 8.0
    temporal_dense_half_window_seconds: float = 2.0
    temporal_dense_min_valley_seconds: float = 0.5
    temporal_dense_windows_per_10_minutes: int = 3
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


def calibrate_candidate_confidence(candidate):
    """Return a candidate with a bounded, explainable evidence score.

    The score is deliberately ordinal rather than a claimed probability. It
    uses only diagnostics produced after candidate generation, so it cannot
    create, remove, split, merge, or move an interval. Dense-validated rescue
    and split evidence is rewarded; audio remains diagnostic context only.
    """
    result = dict(candidate)
    raw_confidence = float(candidate.get(
        "rawConfidence", candidate.get("confidence", 0.0)))
    rescue_applied = bool(candidate.get("rescueApplied"))
    sample_fps = float((candidate.get("rescueSampleFps") or 8.0)
                       if rescue_applied else 2.0)
    support_seconds = float(candidate.get("supportFrames") or 0) / sample_fps
    strong_seconds = float(candidate.get("strongFrames") or 0) / sample_fps
    side_balance = max(0.0, min(1.0, float(
        candidate.get("sideBalance") or 0.0)))
    short_evidence = candidate.get("shortEvidence") or {}
    structural_penalty = max(0.0, float(
        short_evidence.get("penalty") or 0.0))

    # Bilateral evidence is strongest in a target-table-like middle range.
    # Near-perfect symmetry is treated cautiously because the three benchmark
    # scenes show it can be sustained background/camera motion, not a rally.
    bilateral = max(0.0, 1.0 - abs(side_balance - 0.35) / 0.35)
    background_symmetry = max(0.0, min(1.0,
        (side_balance - 0.60) / 0.30))
    components = dict(
        base=0.28,
        sustainedSupport=0.26 * min(1.0, support_seconds / 5.0),
        sustainedStrong=0.10 * min(1.0, strong_seconds / 3.0),
        bilateralTargetEvidence=0.20 * bilateral,
        validatedSplit=0.12 if candidate.get("splitApplied") else 0.0,
        validatedRescue=0.20 if rescue_applied else 0.0,
        backgroundSymmetryCaution=-0.20 * background_symmetry,
        structuralWeakness=-0.50 * structural_penalty,
    )
    score = max(0.0, min(0.99, sum(components.values())))
    confidence = round(score, 2)
    tier = "high" if confidence >= 0.75 else (
        "medium" if confidence >= 0.55 else "low")
    positive = [name for name in (
        "validatedRescue", "validatedSplit", "bilateralTargetEvidence",
        "sustainedSupport", "sustainedStrong")
        if components[name] >= 0.08]
    cautions = [name for name in (
        "backgroundSymmetryCaution", "structuralWeakness")
        if components[name] <= -0.04]
    result.update(
        rawConfidence=round(max(0.0, min(1.0, raw_confidence)), 2),
        confidence=confidence,
        confidenceTier=tier,
        confidenceBasis=dict(
            model="cross-scene-evidence-v1",
            kind="ordinal_evidence_not_probability",
            supportSeconds=round(support_seconds, 3),
            strongSeconds=round(strong_seconds, 3),
            components={name: round(value, 3)
                        for name, value in components.items()},
            positive=positive,
            cautions=cautions,
        ),
    )
    return result


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


def _side_balance(item):
    return min(item["left"], item["right"]) / max(
        .001, item["left"], item["right"])


def _compress_temporal_states(states, metrics, frame_seconds):
    """Return a compact, deterministic trace of the temporal state timeline."""
    if not states:
        return []
    runs = []
    first = 0
    for index in range(1, len(states) + 1):
        if index < len(states) and states[index][1] == states[first][1]:
            continue
        runs.append(dict(
            state=states[first][1],
            start=round(metrics[states[first][0]]["t"], 3),
            end=round(metrics[states[index - 1][0]]["t"] + frame_seconds, 3),
            frames=index - first,
        ))
        first = index
    return runs


def _temporal_state_proposals(group, metrics, smoothed, audio_impacts,
                              threshold, support_threshold, config):
    """Propose split valleys using an idle/active/ending state machine.

    The state model deliberately observes the existing coarse timeline. Motion
    must be bilateral and persistent to enter or restart `active`. A pause may
    remain in `ending` without closing the rally; only a persistent low-evidence
    interval followed by a persistent restart becomes a split proposal.
    """
    frame_seconds = 1 / config.sample_fps
    enter_frames = max(2, math.ceil(
        config.temporal_enter_persistence_seconds * config.sample_fps))
    leave_frames = max(2, math.ceil(
        config.temporal_leave_persistence_seconds * config.sample_fps))
    restart_frames = max(2, math.ceil(
        config.temporal_restart_persistence_seconds * config.sample_fps))
    full = list(range(group[0], group[-1] + 1))
    evidence = {}
    for index in full:
        balance = _side_balance(metrics[index])
        evidence[index] = dict(
            balance=balance,
            enter=(smoothed[index] >= support_threshold
                   and balance >= config.temporal_min_side_balance),
            continue_=(smoothed[index] >= support_threshold
                       and balance >= config.temporal_continue_side_balance),
            strong=(smoothed[index] >= threshold
                    and balance >= config.temporal_min_side_balance),
        )

    states = []
    state = "idle"
    enter_run = []
    restart_run = []
    valley_start = None
    active_seen = False
    proposals = []

    def append_state(index, value):
        states.append((index, value))

    def add_proposal(restart_start):
        nonlocal valley_start
        if valley_start is None or restart_start <= valley_start:
            return
        first, last = valley_start, restart_start - 1
        valley = list(range(first, last + 1))
        duration = len(valley) * frame_seconds
        if duration + 1e-9 < config.temporal_min_valley_seconds:
            return
        nearby = max(2, round(2.5 * config.sample_fps))
        before = [i for i in group if i < first][-nearby:]
        after = [i for i in group if i >= restart_start][:nearby]
        if not before or not after:
            return
        left_span = (metrics[before[-1]]["t"] - metrics[group[0]]["t"]
                     + frame_seconds)
        right_span = (metrics[group[-1]]["t"] - metrics[after[0]]["t"]
                      + frame_seconds)
        left_strong = sum(evidence[i]["strong"] for i in before)
        right_strong = sum(evidence[i]["strong"] for i in after)
        left_balance = sum(evidence[i]["balance"] for i in before) / len(before)
        right_balance = sum(evidence[i]["balance"] for i in after) / len(after)
        low_frames = sum(not evidence[i]["continue_"] for i in valley)
        one_sided = sum(
            evidence[i]["balance"] < config.temporal_continue_side_balance
            for i in valley)
        low_ratio = low_frames / len(valley)
        hits = sum(metrics[first]["t"] - .1 <= t <=
                   metrics[last]["t"] + frame_seconds + .1
                   for t in audio_impacts)
        score = sum(smoothed[i] for i in valley) / len(valley)
        flank = min(max(smoothed[i] for i in before),
                    max(smoothed[i] for i in after))
        depth = max(0.0, 1.0 - score / max(.001, flank))
        confidence = min(1.0, duration / 1.5) * .26
        confidence += low_ratio * .24
        confidence += min(1.0, left_strong / 2) * .14
        confidence += min(1.0, right_strong / 2) * .20
        confidence += min(1.0, depth / .45) * .16
        confidence -= min(3, hits) * .025 * min(1.0, 1.5 / duration)
        point = (metrics[first]["t"] + metrics[last]["t"]) / 2
        proposals.append(dict(
            first=first, last=last, point=round(point, 3),
            motionValleyScore=round(score, 2),
            motionValleyDuration=round(duration, 2), audioHits=hits,
            valleyDepth=round(depth, 3),
            visualConfidence=round(confidence, 3), audioPenalty=0.0,
            splitConfidence=round(max(0.0, confidence), 3),
            stateSplitConfidence=round(max(0.0, confidence), 3),
            stateLowRatio=round(low_ratio, 3),
            stateOneSidedFrames=one_sided,
            stateLeftStrongFrames=left_strong,
            stateRightStrongFrames=right_strong,
            stateLeftBalance=round(left_balance, 3),
            stateRightBalance=round(right_balance, 3),
            stateLeftSpan=round(left_span, 3),
            stateRightSpan=round(right_span, 3),
        ))

    for position, index in enumerate(full):
        row = evidence[index]
        if state == "idle":
            if row["enter"]:
                enter_run.append(index)
            else:
                enter_run = []
            if len(enter_run) >= enter_frames:
                restart_start = enter_run[-enter_frames]
                if active_seen and valley_start is not None:
                    add_proposal(restart_start)
                state = "active"
                active_seen = True
                valley_start = None
                enter_run = []
                restart_run = []
            append_state(index, state)
            continue

        if state == "active":
            if row["continue_"]:
                append_state(index, "active")
                continue
            state = "ending"
            valley_start = index
            restart_run = []
            append_state(index, "ending")
            continue

        # `ending` tolerates a brief pause. After persistent low evidence the
        # trace becomes idle, but the pending valley remains available until a
        # persistent bilateral restart appears.
        if row["enter"]:
            restart_run.append(index)
        else:
            restart_run = []
        low_length = index - valley_start + 1 if valley_start is not None else 0
        if len(restart_run) >= restart_frames:
            restart_start = restart_run[-restart_frames]
            if restart_start - valley_start < leave_frames:
                state = "active"
            else:
                add_proposal(restart_start)
                state = "active"
            valley_start = None
            restart_run = []
        elif low_length >= leave_frames:
            state = "idle"
            enter_run = []
            # Keep valley_start: idle after an active rally is different from
            # the initial idle state and can still produce a split on restart.
        append_state(index, state)

    trace = _compress_temporal_states(states, metrics, frame_seconds)
    for proposal in proposals:
        proposal["temporalStates"] = trace
    return proposals, trace


def analyze_temporal_split_window(metrics, boundary, audio_impacts=None,
                                  config=None):
    """Validate one suspected split boundary in a small 8 fps window."""
    config = config or DetectorConfig()
    audio_impacts = audio_impacts or []
    if len(metrics) < 8:
        return dict(passed=False, reason="insufficient_dense_frames",
                    boundary=round(boundary, 3))
    raw = [item["motion"] for item in metrics]
    smoothed = _smooth(raw)
    baseline = percentile(smoothed, .25)
    activity = percentile(smoothed, .88)
    delta = max(.28, (activity - baseline) * .42)
    threshold = baseline + delta
    support = baseline + max(.16, delta * .48)
    bilateral = [_side_balance(item) >= config.temporal_min_side_balance
                 for item in metrics]
    active = [value >= support and bilateral[index]
              for index, value in enumerate(smoothed)]
    strong = [value >= threshold and bilateral[index]
              for index, value in enumerate(smoothed)]

    runs, run = [], []
    for index, is_active in enumerate(active):
        if not is_active:
            run.append(index)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    max_distance = .8
    candidates = []
    for valley in runs:
        start_t = metrics[valley[0]]["t"]
        end_t = metrics[valley[-1]]["t"] + 1 / config.temporal_dense_fps
        if start_t <= boundary <= end_t:
            distance = 0.0
        else:
            distance = min(abs(boundary - start_t), abs(boundary - end_t))
        duration = end_t - start_t
        if distance <= max_distance and duration + 1e-9 >= (
                config.temporal_dense_min_valley_seconds):
            candidates.append((distance, -duration, valley))
    if not candidates:
        return dict(passed=False, reason="no_persistent_dense_valley",
                    boundary=round(boundary, 3),
                    denseThreshold=round(threshold, 3),
                    denseSupportThreshold=round(support, 3))
    _, _, valley = min(candidates)
    first, last = valley[0], valley[-1]
    flank_frames = max(4, round(1.25 * config.temporal_dense_fps))
    before = list(range(max(0, first - flank_frames), first))
    after = list(range(last + 1, min(len(metrics), last + 1 + flank_frames)))
    left_strong = sum(strong[i] for i in before)
    right_strong = sum(strong[i] for i in after)
    left_support = sum(active[i] for i in before)
    right_support = sum(active[i] for i in after)
    valley_duration = (last - first + 1) / config.temporal_dense_fps
    hits = sum(metrics[first]["t"] - .08 <= t <=
               metrics[last]["t"] + 1 / config.temporal_dense_fps + .08
               for t in audio_impacts)
    complete_sides = (left_strong >= 2 and right_strong >= 2
                      and left_support >= 3 and right_support >= 3)
    passed = complete_sides
    confidence = min(1.0, valley_duration / 1.0) * .45
    confidence += min(1.0, left_strong / 3) * .20
    confidence += min(1.0, right_strong / 3) * .25
    confidence -= min(hits, 3) * .025
    return dict(
        passed=passed,
        reason=("persistent_dense_valley_bilateral_restart" if passed
                else "dense_flanks_insufficient"),
        boundary=round(boundary, 3),
        denseBoundary=round((metrics[first]["t"] + metrics[last]["t"]) / 2, 3),
        denseValleyStart=round(metrics[first]["t"], 3),
        denseValleyEnd=round(metrics[last]["t"]
                             + 1 / config.temporal_dense_fps, 3),
        denseValleyDuration=round(valley_duration, 3),
        denseLeftStrongFrames=left_strong,
        denseRightStrongFrames=right_strong,
        denseLeftSupportFrames=left_support,
        denseRightSupportFrames=right_support,
        denseAudioHits=hits,
        denseConfidence=round(max(0.0, confidence), 3),
        denseThreshold=round(threshold, 3),
        denseSupportThreshold=round(support, 3),
    )


def _split_on_motion_valleys(group, metrics, raw, smoothed, audio_impacts,
                             threshold, config, support_threshold=None,
                             dense_split_evidence=None):
    """Split a long visual group only at sustained valleys with visual restart.

    Audio never proposes or unconditionally vetoes a split. It only discounts
    confidence in a short pause; sustained visual evidence remains primary.
    """
    frame_seconds = 1 / config.sample_fps
    support_threshold = (threshold * .85 if support_threshold is None
                         else support_threshold)
    span = (metrics[group[-1]]["t"] - metrics[group[0]]["t"]
            + frame_seconds)
    dense_split_evidence = dense_split_evidence or {}
    temporal_proposals, temporal_trace = _temporal_state_proposals(
        group, metrics, smoothed, audio_impacts, threshold,
        support_threshold, config)
    legacy_enabled = span >= config.split_min_group_seconds

    low_limit = threshold * config.split_valley_threshold_ratio
    min_valley_frames = max(2, math.ceil(config.split_min_valley_seconds
                                          * config.sample_fps))
    nearby = max(2, round(2.5 * config.sample_fps))
    full = range(group[0], group[-1] + 1)
    runs = []
    run = []
    for index in full if legacy_enabled else ():
        if raw[index] <= low_limit:
            run.append(index)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)

    checks = []
    viable = []
    check_bounds = {}
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
                     splitConfidence=None, antiMergeGuard=False,
                     decision="keep", reason="")
        checks.append(check)
        check_bounds[id(check)] = (first, last)
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
        anti_merge_guard = (
            span >= config.split_anti_merge_min_group_seconds
            and duration >= config.split_anti_merge_min_valley_seconds
            and hits == 0
            and score <= flank_peak * config.split_anti_merge_valley_peak_ratio
            and min(left_peak, right_peak) >= threshold
            and max(left_peak, right_peak)
            >= threshold * config.split_anti_merge_restart_ratio
        )
        check["antiMergeGuard"] = anti_merge_guard
        if (score > min(left_peak, right_peak) * config.split_valley_peak_ratio
                and not anti_merge_guard):
            check["reason"] = "shallow_valley"
            continue
        restart_ratio = (1.0 if anti_merge_guard
                         else config.split_restart_peak_ratio)
        minimum_flank_frames = 1 if anti_merge_guard else 2
        if (right_peak < threshold * restart_ratio
                or sum(smoothed[i] >= threshold for i in after[:nearby])
                < minimum_flank_frames):
            check["reason"] = "no_visual_restart"
            continue
        if (left_peak < threshold * restart_ratio
                or sum(smoothed[i] >= threshold for i in before[-nearby:])
                < minimum_flank_frames):
            check["reason"] = "weak_visual_before"
            continue
        if (split_confidence < config.split_confidence_threshold
                and not anti_merge_guard):
            check["reason"] = "low_split_confidence"
            continue
        viable.append((first, last, check))

    for proposal in temporal_proposals:
        first, last = proposal.pop("first"), proposal.pop("last")
        closest = min(checks, key=lambda item: abs(
            item.get("point", float("inf")) - proposal["point"])) if checks else None
        if closest is not None and abs(closest.get("point", 0)
                                       - proposal["point"]) <= 1.0:
            check = closest
            check.update({key: value for key, value in proposal.items()
                          if key.startswith("state") or key == "temporalStates"})
            check["temporalCorroborated"] = True
        else:
            check = dict(proposal, antiMergeGuard=False,
                         decision="keep", reason="")
            check["temporalState"] = True
            checks.append(check)
        check_bounds[id(check)] = (first, last)
        if any(existing[2] is check for existing in viable):
            continue
        check["temporalState"] = True
        child_ok = (proposal["stateLeftSpan"] + 1e-9
                    >= config.temporal_min_child_seconds
                    and proposal["stateRightSpan"] + 1e-9
                    >= config.temporal_min_child_seconds)
        flank_ok = (proposal["stateLeftStrongFrames"] >= 1
                    and proposal["stateRightStrongFrames"] >= 1
                    and proposal["stateLeftBalance"]
                    >= config.temporal_min_side_balance
                    and proposal["stateRightBalance"]
                    >= config.temporal_min_side_balance)
        direct = (
            proposal["motionValleyDuration"] + 1e-9
            >= config.temporal_direct_valley_seconds
            and proposal["stateLowRatio"] >= .67
            and proposal["stateLeftStrongFrames"] >= 2
            and proposal["stateRightStrongFrames"] >= 2
            and proposal["splitConfidence"]
            >= config.temporal_direct_confidence
            and proposal["audioHits"] <= 3
            and proposal["stateLeftSpan"] + 1e-9
            >= config.split_min_side_seconds
            and proposal["stateRightSpan"] + 1e-9
            >= config.split_min_side_seconds
            and child_ok and flank_ok
        )
        dense = dense_split_evidence.get(f"{proposal['point']:.3f}")
        if direct:
            nearby_viable = [item for item in viable
                             if item[2].get("point") is not None
                             and abs(item[2]["point"] - proposal["point"])
                             <= 4.0]
            if nearby_viable:
                check.update(reason="temporal_redundant_near_coarse_split",
                             denseCheckRequested=False,
                             denseSplitEvidence=None)
                continue
            check.update(decision="keep", reason="temporal_state_clear_valley",
                         denseCheckRequested=False, denseSplitEvidence=None)
            viable.append((first, last, check))
            continue
        if not child_ok:
            check.update(reason="temporal_short_child",
                         denseCheckRequested=False, denseSplitEvidence=None)
            continue
        if not flank_ok or proposal["splitConfidence"] < (
                config.temporal_dense_confidence):
            check.update(reason="temporal_weak_flanks",
                         denseCheckRequested=False, denseSplitEvidence=None)
            continue
        check.update(reason="temporal_state_ambiguous",
                     denseCheckRequested=False, denseSplitEvidence=dense)

    # A coarse state transition may be too weak to split directly while still
    # identifying where a long, joined candidate deserves a local re-check.
    # Restrict that re-check to central structural valleys: this avoids dense
    # scans of ordinary pauses and prevents short candidates from fragmenting.
    group_start = metrics[group[0]]["t"]
    group_end = metrics[group[-1]]["t"] + frame_seconds
    group_audio_hits = sum(group_start - config.pre_roll_seconds <= impact <=
                           group_end + config.post_roll_seconds
                           for impact in audio_impacts)
    state_points = [proposal["point"] for proposal in temporal_proposals]
    structural_request_points = []
    if (span >= config.split_anti_merge_min_group_seconds
            and group_audio_hits >= config.auxiliary_audio_hits):
        for check in checks:
            if any(item[2] is check for item in viable):
                continue
            point = check.get("point")
            bounds = check_bounds.get(id(check))
            confidence = check.get("splitConfidence") or 0.0
            if point is None or bounds is None or confidence < .50:
                continue
            left_span = point - group_start
            right_span = group_end - point
            minimum_side = min(left_span, right_span)
            central = (minimum_side + 1e-9 >= config.split_min_side_seconds
                       and minimum_side / max(.001, span) >= .30)
            nearby_state = any(abs(point - candidate) <= 3.5
                               for candidate in state_points)
            existing_points = [item[2].get("point") for item in viable]
            existing_points = [value for value in existing_points
                               if value is not None]
            short_second_split = (
                check.get("reason") == "brief_valley"
                and config.temporal_min_child_seconds <= minimum_side
                <= config.split_min_side_seconds
                and any(point - value >= config.split_min_side_seconds
                        for value in existing_points)
            )
            structural = (
                central and (
                    (check.get("reason") in (
                        "low_split_confidence", "shallow_valley")
                     and (check.get("motionValleyDuration") or 0) >= 1.0)
                    or (check.get("reason") == "temporal_state_ambiguous"
                        and (check.get("motionValleyDuration") or 0) >= 1.0)
                    or (check.get("reason") == "brief_valley"
                        and nearby_state)
                )
            ) or short_second_split
            if not structural:
                continue
            if (not short_second_split
                    and any(abs(point - value) <= 3.0
                            for value in existing_points)):
                continue
            if any(abs(point - value) <= 4.0
                   for value in structural_request_points):
                continue
            structural_request_points.append(point)
            check["structuralRefinement"] = True
            check["structuralPriority"] = (1.0 if short_second_split else
                                           .2 if check.get("temporalState")
                                           else 0.0)
            dense = dense_split_evidence.get(f"{point:.3f}")
            check["denseSplitEvidence"] = dense
            if dense is None:
                check.update(reason="structural_dense_recheck_required",
                             denseCheckRequested=True)
                continue
            check["denseCheckRequested"] = False
            if not dense.get("passed"):
                check["reason"] = dense.get(
                    "reason", "structural_dense_recheck_failed")
                continue
            check.update(
                reason="structural_dense_valley_restart",
                splitConfidence=round(max(confidence,
                                          dense.get("denseConfidence", 0)), 3),
            )
            viable.append((*bounds, check))

    for check in checks:
        check.setdefault("temporalStates", temporal_trace)
        check.setdefault("temporalState", False)
        check.setdefault("denseCheckRequested", False)
        check.setdefault("denseSplitEvidence", None)

    if not viable:
        if not checks:
            reason = ("short_candidate" if span < config.temporal_min_group_seconds
                      else "no_sustained_valley")
            checks = [dict(decision="keep", reason=reason,
                           temporalStates=temporal_trace,
                           temporalState=span >= config.temporal_min_group_seconds,
                           denseCheckRequested=False)]
        return [(group, None)], checks

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
            minimum_span = (config.temporal_min_child_seconds
                            if (check.get("temporalState")
                                or check.get("structuralRefinement"))
                            else config.split_min_side_seconds)
            if (metrics[piece[-1]]["t"] - metrics[piece[0]]["t"]
                    + frame_seconds < minimum_span):
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
        if check.get("temporalState"):
            check["reason"] = (
                "structural_dense_valley_restart"
                if check.get("structuralRefinement")
                else "temporal_state_clear_valley"
            )
        elif check.get("structuralRefinement"):
            check["reason"] = "structural_dense_valley_restart"
        else:
            check["reason"] = (
                "long_candidate_clear_gap_visual_restart"
                if check.get("antiMergeGuard")
                else "sustained_motion_valley_visual_restart"
            )

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


def analyze_motion(metrics, audio_impacts=None, config=None, duration=None,
                   dense_split_evidence=None):
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
            group, metrics, raw, smoothed, audio_impacts, threshold,
            config, support_threshold, dense_split_evidence)
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
            antiMergeGuardApplied=bool(split_check.get("antiMergeGuard")),
            antiMergeBasis=(split_check.get("reason")
                            if split_check.get("antiMergeGuard") else None),
            splitDecision=split_check["decision"],
            splitReason=split_check["reason"], splitChecks=split_checks,
            temporalStates=split_check.get("temporalStates", []),
            splitApplied=split_check["decision"] == "split",
            splitBasis=(split_check.get("reason")
                        if split_check["decision"] == "split" else None),
            splitBoundary=(split_check.get("point")
                           if split_check["decision"] == "split" else None),
            splitEvidence=dict(
                source=("temporal_state" if split_check.get("temporalState")
                        else "legacy_motion_valley"),
                stateLowRatio=split_check.get("stateLowRatio"),
                stateOneSidedFrames=split_check.get("stateOneSidedFrames"),
                dense=split_check.get("denseSplitEvidence"),
            ),
        ))
    candidates.sort(key=lambda item: (item["start"], item["end"]))
    for left, right in zip(candidates, candidates[1:]):
        if left["end"] < right["start"]:
            continue
        if not (left.get("splitApplied") or right.get("splitApplied")):
            continue
        original_end = left["end"]
        adjusted_end = round(max(left["start"], right["start"] - .001), 3)
        if adjusted_end >= original_end:
            continue
        left["end"] = adjusted_end
        left["duration"] = round(adjusted_end - left["start"], 3)
        left["splitOverlapTrimmed"] = True
        left["splitOverlapOriginalEnd"] = original_end
    for candidate in candidates:
        candidate.setdefault("splitOverlapTrimmed", False)
        candidate.setdefault("splitOverlapOriginalEnd", None)
    dense_requests = {}
    for check in all_split_checks:
        if not check.get("denseCheckRequested") or check.get("point") is None:
            continue
        key = f"{check['point']:.3f}"
        priority = ((check.get("splitConfidence") or 0)
                    + min(2.0, check.get("motionValleyDuration") or 0) * .05
                    + (check.get("structuralPriority") or 0))
        if key not in dense_requests or priority > dense_requests[key]["priority"]:
            dense_requests[key] = dict(
                point=round(check["point"], 3),
                priority=round(priority, 3),
                coarseConfidence=check.get("splitConfidence"),
                coarseValleyDuration=check.get("motionValleyDuration"),
            )
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
        temporalStateSplits=sum(
            check["decision"] == "split" and check.get("temporalState")
            for check in all_split_checks),
        temporalDenseCheckPoints=sorted(
            dense_requests.values(), key=lambda item: (-item["priority"],
                                                        item["point"])),
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
                           duration=None, include_diagnostics=False):
    """Return short local-rescan windows backed by cheap audio/motion evidence.

    A2 retains the original three-hit bilateral trigger and adds two narrow
    alternatives: a separated two-hit pair with clearly bilateral coarse
    motion, and a compact strong cluster whose coarse frame is only slightly
    one-sided. Both alternatives still require the existing dense visual gate.
    """
    config = config or DetectorConfig()
    if not metrics or not audio_impacts:
        empty = {"accepted": 0, "byTrigger": {}, "rejections": {}}
        return ([], empty) if include_diagnostics else []
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
    rejection_counts = {}

    def reject(reason):
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    def evidence(cluster, trigger, minimum_balance, minimum_dense_hits,
                 relaxed_adjacency=False, post_padding=None):
        core_start = max(0.0, cluster[0] - .12)
        core_end = min(stop, cluster[-1] + .12)
        start = max(0.0, core_start - config.rescue_window_padding_seconds)
        end = min(stop, core_end + (
            config.rescue_window_padding_seconds
            if post_padding is None else post_padding))
        nearby_candidates = []
        for item in candidates:
            overlap = _interval_overlap(start, end, item["start"], item["end"])
            gap = _interval_gap(core_start, core_end, item["start"], item["end"])
            blocked = (overlap >= config.rescue_overlap_guard_seconds
                       or gap <= (config.rescue_visual_gap_seconds
                                  if relaxed_adjacency
                                  else config.rescue_adjacent_guard_seconds))
            if blocked:
                nearby_candidates.append(item)
        if nearby_candidates:
            reject("existing_candidate_guard")
            return
        nearby = [i for i, item in enumerate(metrics)
                  if start - frame_seconds <= item["t"] <= end + frame_seconds]
        elevated = [i for i in nearby if smoothed[i] >= proposal_floor]
        if not elevated:
            reject("coarse_motion_below_floor")
            return
        # Smoothing spreads one burst into adjacent quiet frames. Use the raw
        # peak for the bilateral check so an idle neighbour cannot make a
        # one-sided walk look balanced.
        strongest = max(elevated, key=lambda i: raw[i])
        side_balance = (min(metrics[strongest]["left"], metrics[strongest]["right"])
                        / max(.001, metrics[strongest]["left"],
                              metrics[strongest]["right"]))
        if side_balance < minimum_balance:
            reject("coarse_side_balance")
            return
        windows.append(dict(
            start=round(start, 3), end=round(end, 3),
            coreStart=round(core_start, 3), coreEnd=round(core_end, 3),
            audioHits=len(cluster), coarseMotionPeak=round(smoothed[strongest], 2),
            coarseMotionRatio=round(smoothed[strongest] / max(.001, proposal_floor), 2),
            coarseSideBalance=round(side_balance, 3),
            rescueTrigger=trigger, minDenseAudioHits=minimum_dense_hits,
            audioTimes=list(cluster),
        ))

    clusters = _audio_clusters(
        audio_impacts, config.rescue_audio_cluster_gap_seconds)
    for cluster in clusters:
        if len(cluster) < config.rescue_min_audio_hits:
            reject("audio_hits_below_standard")
            continue
        audio_span = cluster[-1] - cluster[0]
        if audio_span > config.rescue_max_audio_span_seconds:
            reject("audio_span_too_long")
            continue
        # The standard rule is evaluated first. The compact strong-cluster
        # alternative relaxes only the cheap coarse balance, never the dense
        # visual balance required to create a candidate.
        raw_nearby = [i for i, item in enumerate(metrics)
                      if cluster[0] - config.rescue_window_padding_seconds
                      - frame_seconds <= item["t"]
                      <= cluster[-1] + config.rescue_window_padding_seconds
                      + frame_seconds]
        elevated = [i for i in raw_nearby if smoothed[i] >= proposal_floor]
        balance = None
        if elevated:
            strongest = max(elevated, key=lambda i: raw[i])
            balance = (min(metrics[strongest]["left"], metrics[strongest]["right"])
                       / max(.001, metrics[strongest]["left"],
                             metrics[strongest]["right"]))
        standard_minimum = config.rescue_min_side_balance * .65
        if balance is not None and balance >= standard_minimum:
            evidence(cluster, "audio-cluster+bilateral-coarse-motion",
                     standard_minimum, config.rescue_min_audio_hits)
        elif (len(cluster) >= config.rescue_alt_min_audio_hits
              and audio_span <= config.rescue_alt_max_audio_span_seconds):
            evidence(cluster, "strong-audio+brief-one-sided-coarse-motion",
                     config.rescue_alt_min_coarse_side_balance,
                     2,
                     post_padding=config.rescue_alt_post_padding_seconds)
        else:
            reject("coarse_side_balance")

    # Two individually isolated impacts may still describe a very short rally
    # when their separation only narrowly exceeds the standard cluster gap.
    # Requiring two singleton clusters and bilateral coarse evidence keeps this
    # from turning ordinary background audio into dense-decode work.
    for left, right in zip(clusters, clusters[1:]):
        if len(left) != 1 or len(right) != 1:
            continue
        gap = right[0] - left[0]
        if not (config.rescue_audio_cluster_gap_seconds < gap
                <= config.rescue_audio_pair_max_gap_seconds):
            continue
        evidence([left[0], right[0]],
                 "separated-audio-pair+bilateral-coarse-motion",
                 config.rescue_min_side_balance, 2,
                 relaxed_adjacency=True)

    # Overlapping audio clusters describe one suspicious episode. Keep one
    # dense decode and retain the combined core/evidence.
    merged = []
    for window in sorted(windows, key=lambda item: (item["start"], item["end"])):
        if not merged or window["start"] > merged[-1]["end"]:
            merged.append(window)
            continue
        previous = merged[-1]
        previous["end"] = max(previous["end"], window["end"])
        previous["coreStart"] = min(previous["coreStart"], window["coreStart"])
        previous["coreEnd"] = max(previous["coreEnd"], window["coreEnd"])
        previous["audioTimes"] = sorted(set(previous["audioTimes"]
                                             + window["audioTimes"]))
        previous["audioHits"] = len(previous["audioTimes"])
        triggers = set(previous["rescueTrigger"].split("+merged+"))
        triggers.add(window["rescueTrigger"])
        previous["rescueTrigger"] = "+merged+".join(sorted(triggers))
        previous["minDenseAudioHits"] = min(
            previous["minDenseAudioHits"], window["minDenseAudioHits"])
        if window["coarseMotionPeak"] > previous["coarseMotionPeak"]:
            previous["coarseMotionPeak"] = window["coarseMotionPeak"]
            previous["coarseMotionRatio"] = window["coarseMotionRatio"]
            previous["coarseSideBalance"] = window["coarseSideBalance"]
    for window in merged:
        window.pop("audioTimes", None)
    trigger_counts = {}
    for window in merged:
        trigger = window["rescueTrigger"]
        trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1
    diagnostics = dict(accepted=len(merged), byTrigger=trigger_counts,
                       rejections=rejection_counts)
    return (merged, diagnostics) if include_diagnostics else merged


def analyze_rescue_window_candidates(metrics, audio_impacts, proposal,
                                     config=None, duration=None,
                                     include_diagnostics=False):
    """Validate and keep separate dense bursts inside one rescue window."""
    config = config or DetectorConfig()
    if len(metrics) < 4:
        empty = {"groups": 0, "accepted": 0,
                 "rejections": {"insufficient_frames": 1}}
        return ([], empty) if include_diagnostics else []
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
    accepted = []
    rejection_counts = {}

    def reject(reason):
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    for group_index, group in enumerate(groups):
        first, last = group[0], group[-1]
        visual_start = metrics[first]["t"]
        visual_end = metrics[last]["t"]
        visual_span = visual_end - visual_start + 1 / config.rescue_fps
        if visual_span < config.rescue_min_visual_seconds:
            reject("visual_span_too_short")
            continue
        if _interval_overlap(visual_start, visual_end + 1 / config.rescue_fps,
                             proposal["coreStart"], proposal["coreEnd"]) <= 0:
            reject("outside_audio_core")
            continue
        strong = [i for i in group if smoothed[i] >= threshold]
        if len(strong) < max(2, math.ceil(
                config.rescue_min_strong_seconds * config.rescue_fps)):
            reject("insufficient_strong_frames")
            continue
        window = metrics[first:last + 1]
        left_mean = sum(item["left"] for item in window) / len(window)
        right_mean = sum(item["right"] for item in window) / len(window)
        side_balance = min(left_mean, right_mean) / max(.001, left_mean, right_mean)
        if side_balance < config.rescue_min_side_balance:
            reject("dense_side_balance")
            continue
        start = max(0.0, visual_start - config.rescue_pre_roll_seconds)
        stop = duration if duration is not None else metrics[-1]["t"] + 1 / config.rescue_fps
        end = min(stop, visual_end + config.rescue_post_roll_seconds)
        if end - start > config.rescue_max_candidate_seconds:
            reject("candidate_too_long")
            continue
        hits = [t for t in audio_impacts if start - .08 <= t <= end + .08]
        if len(hits) < proposal.get("minDenseAudioHits",
                                    config.rescue_min_audio_hits):
            reject("dense_audio_hits")
            continue
        pre = smoothed[max(0, first - 4):first]
        post = smoothed[last + 1:min(len(smoothed), last + 5)]
        motion_peak = max(smoothed[first:last + 1])
        peak_threshold_ratio = motion_peak / max(.001, threshold)
        # v3.0-A targets brief rallies. A rescue that grows beyond two seconds
        # needs a distinctly stronger local visual peak; otherwise it is more
        # likely to be between-rally walking/reset activity.
        if end - start > 2.0 and peak_threshold_ratio < config.rescue_long_peak_ratio:
            reject("long_candidate_weak_peak")
            continue
        rise = motion_peak - (sum(pre) / len(pre) if pre else baseline)
        fall = motion_peak - (sum(post) / len(post) if post else baseline)
        # A complete local burst is the main guard against nearby walking or
        # continuous background-table motion.
        if rise < delta * .20 or fall < delta * .20:
            reject("incomplete_rise_fall")
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
            rescueBasis=(proposal.get("rescueTrigger",
                                      "audio-cluster+bilateral-coarse-motion")
                         + "+dense-motion"),
            rescueTrigger=proposal.get("rescueTrigger"),
            rescueWindow=[proposal["start"], proposal["end"]],
            rescueAudioHits=len(hits), rescueSampleFps=config.rescue_fps,
            rescueCoarseMotionPeak=proposal.get("coarseMotionPeak"),
            rescueCoarseMotionRatio=proposal.get("coarseMotionRatio"),
            rescueCoarseSideBalance=proposal.get("coarseSideBalance"),
            rescueValleyGuard=dict(
                denseGroups=len(groups), groupIndex=group_index,
                decision=("kept_as_separate_burst" if len(groups) > 1
                          else "single_dense_burst")),
            rescueUnionDecision=None,
            refinedEnd=False, originalEnd=round(end, 3), endRefineBasis=None,
        )
        accepted.append(candidate)
    candidates = sorted(accepted, key=lambda item: (item["start"], item["end"]))
    diagnostics = dict(groups=len(groups), accepted=len(candidates),
                       rejections=rejection_counts,
                       trigger=proposal.get("rescueTrigger"),
                       window=[proposal["start"], proposal["end"]])
    return (candidates, diagnostics) if include_diagnostics else candidates


def analyze_rescue_window(metrics, audio_impacts, proposal, config=None,
                          duration=None):
    """Backward-compatible single-best rescue-window validator."""
    candidates = analyze_rescue_window_candidates(
        metrics, audio_impacts, proposal, config, duration)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (
        item["audioHits"], item["strongFrames"], item["sideBalance"],
        item["motionPeak"]))


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
    result.setdefault("rescueTrigger", None)
    result.setdefault("rescueValleyGuard", None)
    result.setdefault("rescueUnionDecision", None)
    result.setdefault("antiMergeGuardApplied", False)
    result.setdefault("antiMergeBasis", None)
    result.setdefault("temporalStates", [])
    result.setdefault("splitApplied", result.get("splitDecision") == "split")
    result.setdefault("splitBasis", (result.get("splitReason")
                                      if result.get("splitDecision") == "split"
                                      else None))
    result.setdefault("splitBoundary", result.get("splitPoint"))
    result.setdefault("splitEvidence", None)
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


def integrate_rescue_candidate(candidate, others, config=None):
    """Keep a rescue separate unless dense visual continuity says duplicate.

    A2 deliberately does not union rescue and coarse ranges. A small overlap
    caused only by pre/post-roll is trimmed at the dense visual gap; stronger
    overlap is treated as a duplicate and rejected.
    """
    config = config or DetectorConfig()
    result = dict(candidate)
    nearest_gap = None
    for item in others:
        overlap = _interval_overlap(result["start"], result["end"],
                                    item["start"], item["end"])
        gap = _interval_gap(result["start"], result["end"],
                            item["start"], item["end"])
        nearest_gap = gap if nearest_gap is None else min(nearest_gap, gap)
        if overlap <= 0:
            continue
        rescue_visual_start = result.get("visualStart", result["start"])
        rescue_visual_end = result.get("visualEnd", result["end"])
        other_visual_start = item.get("visualStart", item["start"])
        other_visual_end = item.get("visualEnd", item["end"])
        if rescue_visual_end <= other_visual_start:
            visual_gap = other_visual_start - rescue_visual_end
            if visual_gap >= config.rescue_visual_gap_seconds:
                boundary = (rescue_visual_end + other_visual_start) / 2
                result["end"] = round(min(result["end"], boundary,
                                          item["start"]), 3)
                result["duration"] = round(result["end"] - result["start"], 3)
                result["rescueUnionDecision"] = "kept_separate_at_dense_valley"
                continue
        elif other_visual_end <= rescue_visual_start:
            visual_gap = rescue_visual_start - other_visual_end
            if visual_gap >= config.rescue_visual_gap_seconds:
                boundary = (other_visual_end + rescue_visual_start) / 2
                result["start"] = round(max(result["start"], boundary,
                                            item["end"]), 3)
                result["duration"] = round(result["end"] - result["start"], 3)
                result["rescueUnionDecision"] = "kept_separate_at_dense_valley"
                continue
        return None, "duplicate_overlap_no_continuity"
    if result.get("rescueUnionDecision") is None:
        result["rescueUnionDecision"] = (
            "adjacent_kept_separate_no_union"
            if nearest_gap is not None
            and nearest_gap <= config.rescue_adjacent_guard_seconds
            else "new_local_candidate"
        )
    return result, result["rescueUnionDecision"]


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
    total_started = time.perf_counter()
    stage_started = time.perf_counter()
    metrics = decode_motion(video_path, ffmpeg_path, roi, config, start, end)
    coarse_decode_seconds = time.perf_counter() - stage_started
    stage_started = time.perf_counter()
    audio_impacts, audio_diagnostics = decode_audio(
        video_path, ffmpeg_path, config, start, end)
    audio_decode_seconds = time.perf_counter() - stage_started
    stop = end if end is not None else duration
    stage_started = time.perf_counter()
    candidates, motion_diagnostics = analyze_motion(
        metrics, audio_impacts, config, stop)
    coarse_analysis_seconds = time.perf_counter() - stage_started
    analyzed_end = metrics[-1]["t"] + 1 / config.sample_fps
    stop = stop if stop is not None else analyzed_end
    local_decode_failures = []

    # Coarse state transitions are cheap. Only the highest-priority ambiguous
    # boundaries receive a small 8 fps window, after which the same coarse
    # analysis is rerun with the local evidence attached.
    video_units = max(1.0, (stop - start) / 600.0)
    temporal_budget = max(1, math.ceil(
        config.temporal_dense_windows_per_10_minutes * video_units))
    temporal_requests = motion_diagnostics.get("temporalDenseCheckPoints", [])
    selected_temporal = temporal_requests[:temporal_budget]
    temporal_evidence = {}
    temporal_dense_decoded_seconds = 0.0
    temporal_dense_wall_seconds = 0.0
    for request in selected_temporal:
        point = request["point"]
        window_start = max(start, point - config.temporal_dense_half_window_seconds)
        window_end = min(stop, point + config.temporal_dense_half_window_seconds)
        if window_end - window_start < 1.0:
            continue
        try:
            stage_started = time.perf_counter()
            dense = decode_motion(
                video_path, ffmpeg_path, roi, config, window_start, window_end,
                sample_fps=config.temporal_dense_fps, keyframes_only=False)
            temporal_dense_wall_seconds += time.perf_counter() - stage_started
        except DetectionError as exc:
            local_decode_failures.append(dict(
                stage="temporal_split", point=point,
                window=[round(window_start, 3), round(window_end, 3)],
                error=str(exc)))
            continue
        temporal_dense_decoded_seconds += window_end - window_start
        temporal_evidence[f"{point:.3f}"] = analyze_temporal_split_window(
            dense, point, audio_impacts, config)
    if temporal_evidence:
        stage_started = time.perf_counter()
        candidates, motion_diagnostics = analyze_motion(
            metrics, audio_impacts, config, stop, temporal_evidence)
        coarse_analysis_seconds += time.perf_counter() - stage_started

    rescue_windows, rescue_proposal_diagnostics = propose_rescue_windows(
        metrics, audio_impacts, candidates, config, stop,
        include_diagnostics=True)
    rescued = []
    rescue_decoded_seconds = 0.0
    rescue_wall_seconds = 0.0
    rescue_union_rejections = {}
    rescue_dense_diagnostics = []
    for window in rescue_windows:
        try:
            stage_started = time.perf_counter()
            dense = decode_motion(
                video_path, ffmpeg_path, roi, config,
                window["start"], window["end"],
                sample_fps=config.rescue_fps, keyframes_only=False)
            rescue_wall_seconds += time.perf_counter() - stage_started
        except DetectionError as exc:
            local_decode_failures.append(dict(stage="rescue", window=window,
                                              error=str(exc)))
            continue
        rescue_decoded_seconds += window["end"] - window["start"]
        dense_candidates, dense_diagnostics = analyze_rescue_window_candidates(
            dense, audio_impacts, window, config, stop,
            include_diagnostics=True)
        rescue_dense_diagnostics.append(dense_diagnostics)
        for candidate in dense_candidates:
            integrated, decision = integrate_rescue_candidate(
                candidate, candidates + rescued, config)
            if integrated is None:
                rescue_union_rejections[decision] = (
                    rescue_union_rejections.get(decision, 0) + 1)
                continue
            rescued.append(integrated)
    candidates.extend(rescued)
    candidates.sort(key=lambda item: (item["start"], item["end"]))

    priorities = []
    for index, candidate in enumerate(candidates):
        priority = _end_refine_priority(candidate, audio_impacts, config)
        if priority is not None:
            priorities.append((priority, index))
    video_minutes = video_units
    end_window_budget = max(1, math.ceil(
        config.end_refine_windows_per_10_minutes * video_minutes))
    selected_end_indices = {index for _, index in sorted(
        priorities, reverse=True)[:end_window_budget]}

    refined = []
    refined_count = 0
    end_decoded_seconds = 0.0
    end_refine_wall_seconds = 0.0
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
            stage_started = time.perf_counter()
            dense = decode_motion(
                video_path, ffmpeg_path, roi, config, window_start, window_end,
                sample_fps=config.end_refine_fps, keyframes_only=False)
            end_refine_wall_seconds += time.perf_counter() - stage_started
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
    candidates = [calibrate_candidate_confidence(candidate)
                  for candidate in refined]
    total_wall_seconds = time.perf_counter() - total_started
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
            rescueProposalDiagnostics=rescue_proposal_diagnostics,
            rescueProposalWindows=rescue_windows,
            rescueDenseDiagnostics=rescue_dense_diagnostics,
            rescueUnionRejections=rescue_union_rejections,
            refinedEndCandidates=refined_count,
            endRefineEligibleCandidates=len(priorities),
            endRefineWindows=len(selected_end_indices),
            temporalDenseRequests=len(temporal_requests),
            temporalDenseWindows=len(selected_temporal),
            temporalDenseEvidence=temporal_evidence,
            temporalDenseSkipped=max(0, len(temporal_requests)
                                     - len(selected_temporal)),
            temporalDenseDecodedSeconds=round(
                temporal_dense_decoded_seconds, 3),
            rescueDecodedSeconds=round(rescue_decoded_seconds, 3),
            endRefineDecodedSeconds=round(end_decoded_seconds, 3),
            runtimeBreakdownSeconds=dict(
                coarseMotionDecode=round(coarse_decode_seconds, 3),
                audioDecode=round(audio_decode_seconds, 3),
                coarseAnalysis=round(coarse_analysis_seconds, 3),
                temporalSplitDenseDecode=round(
                    temporal_dense_wall_seconds, 3),
                rescueDenseDecode=round(rescue_wall_seconds, 3),
                endRefineDenseDecode=round(end_refine_wall_seconds, 3),
                total=round(total_wall_seconds, 3),
            ),
            localDecodeFailures=local_decode_failures,
            note=("主要候選仍由 2 fps ROI 視覺動態產生；B 的 idle/active/ending 狀態"
                  "以持續低證據及雙側重新啟動提出結構切分，只在模糊邊界附近局部進行"
                  "8 fps 驗證。A2 short rescue 與 end refinement 保留；音訊只作佐證，"
                  "既有候選起點修正路徑不變。C 僅將既有診斷轉為 ordinal evidence "
                  "score，不參與候選建立或篩選。"),
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rally Detection v3.0-C：保留 B 偵測，校準候選證據分數。")
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
    print(f"Rally Detection v3.0-C · {result['source']}")
    print("候選由 ROI 影像產生；音訊只作輔助。人工確認前不會建立事件。\n")
    for i, item in enumerate(result["candidates"], 1):
        print(f"{i:3d}  {item['start']:8.2f} → {item['end']:8.2f}  "
              f"duration {item['duration']:5.2f}  "
              f"motion {item['motionMean']:5.2f}/{item['motionPeak']:5.2f}  "
              f"左右 {item['sideBalance']:.2f}  audio {item['audioHits']:2d}  "
              f"frames {item['strongFrames']}/{item['supportFrames']}  "
              f"{item['boundaryBasis']}  evidence {item['confidence']:.0%}"
              f" (raw {item['rawConfidence']:.0%})"
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
            print(f"       short raw confidence {item['baseConfidence']:.0%} → "
                  f"{item['rawConfidence']:.0%} · "
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
