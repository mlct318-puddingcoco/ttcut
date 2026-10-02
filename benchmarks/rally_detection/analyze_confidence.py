"""Analyze candidate confidence against the frozen rally benchmark labels."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from benchmarks.rally_detection.evaluator import (
    EPSILON,
    evaluate_benchmark,
    interval_gap,
    is_primary_match,
    maximum_cardinality_overlap_matching,
    overlap_seconds,
    percentile,
)


def roc_auc(labels: Sequence[bool], scores: Sequence[float]) -> float | None:
    positive = [score for label, score in zip(labels, scores) if label]
    negative = [score for label, score in zip(labels, scores) if not label]
    if not positive or not negative:
        return None
    wins = sum((left > right) + 0.5 * (left == right)
               for left in positive for right in negative)
    return wins / (len(positive) * len(negative))


def average_precision(labels: Sequence[bool], scores: Sequence[float]) -> float | None:
    positives = sum(labels)
    if not positives:
        return None
    grouped = {}
    for label, score in zip(labels, scores):
        count, matched = grouped.get(float(score), (0, 0))
        grouped[float(score)] = (count + 1, matched + int(label))
    found = 0
    seen = 0
    total = 0.0
    for score in sorted(grouped, reverse=True):
        count, matched = grouped[score]
        seen += count
        found += matched
        total += (matched / positives) * (found / seen)
    return total


def brier_score(labels: Sequence[bool], scores: Sequence[float]) -> float | None:
    if not labels:
        return None
    return sum((score - float(label)) ** 2
               for label, score in zip(labels, scores)) / len(labels)


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _distribution(values: Iterable[float]) -> Mapping[str, float | None]:
    values = list(values)
    return {
        "q1": _rounded(percentile(values, 0.25)),
        "median": _rounded(statistics.median(values) if values else None),
        "q3": _rounded(percentile(values, 0.75)),
    }


def _score_summary(rows: Sequence[Mapping[str, Any]], field: str) -> Mapping[str, Any]:
    labels = [bool(row["matched"]) for row in rows]
    scores = [float(row[field]) for row in rows]
    return {
        "matched": _distribution(score for label, score in zip(labels, scores) if label),
        "unmatched": _distribution(score for label, score in zip(labels, scores)
                                   if not label),
        "roc_auc": _rounded(roc_auc(labels, scores)),
        "pr_auc": _rounded(average_precision(labels, scores)),
        "brier": _rounded(brier_score(labels, scores)),
    }


def _calibration_buckets(rows: Sequence[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    result = []
    for lower, upper in ((0.0, 0.4), (0.4, 0.55), (0.55, 0.7),
                         (0.7, 0.85), (0.85, 1.000001)):
        selected = [row for row in rows
                    if lower <= float(row[field]) < upper]
        matched = sum(bool(row["matched"]) for row in selected)
        result.append({
            "range": f"{lower:.2f}-{min(upper, 1.0):.2f}",
            "count": len(selected),
            "mean_score": _rounded(sum(float(row[field]) for row in selected)
                                   / len(selected) if selected else None),
            "matched": matched,
            "precision": _rounded(matched / len(selected) if selected else None),
        })
    return result


def _threshold_sweep(manifest_entry: Mapping[str, Any],
                     rows: Sequence[Mapping[str, Any]],
                     field: str) -> list[dict[str, Any]]:
    result = []
    for threshold in (0.0, 0.4, 0.5, 0.55, 0.6,
                      0.65, 0.7, 0.75, 0.8, 0.9):
        retained = [row["candidate"] for row in rows
                    if float(row[field]) + EPSILON >= threshold]
        evaluated = evaluate_benchmark(
            manifest_entry, retained, include_confidence=False)
        result.append({
            "minimum_confidence": threshold,
            "kept": len(retained),
            "tp": evaluated["tp"],
            "fn": evaluated["fn"],
            "unmatched": evaluated["unmatched"],
            "precision": _rounded(evaluated["precision"]),
            "recall": _rounded(evaluated["recall"]),
            "f1": _rounded(evaluated["f1"]),
        })
    return result


def _bottom_bucket(rows: Sequence[Mapping[str, Any]],
                   field: str, fraction: float = 0.25) -> Mapping[str, Any]:
    if not rows:
        return {"count": 0, "unmatched_rate": None,
                "near_miss_rate": None, "unmatched_enrichment": None}
    count = max(1, math.ceil(len(rows) * fraction))
    selected = sorted(rows, key=lambda row: (
        float(row[field]), row["benchmark_id"], row["prediction_index"]))[:count]
    unmatched_rate = sum(not row["matched"] for row in selected) / count
    near_miss_rate = sum(bool(row["near_miss"]) for row in selected) / count
    overall_unmatched = sum(not row["matched"] for row in rows) / len(rows)
    return {
        "fraction": fraction,
        "count": count,
        "maximum_score": _rounded(max(float(row[field]) for row in selected)),
        "unmatched_rate": _rounded(unmatched_rate),
        "near_miss_rate": _rounded(near_miss_rate),
        "unmatched_enrichment": _rounded(
            unmatched_rate / overall_unmatched if overall_unmatched else None),
    }


def _aggregate_sweeps(by_benchmark: Mapping[str, Mapping[str, Any]],
                      key: str) -> list[dict[str, Any]]:
    sweeps = [benchmark[key] for benchmark in by_benchmark.values()]
    result = []
    for rows in zip(*sweeps):
        kept = sum(row["kept"] for row in rows)
        tp = sum(row["tp"] for row in rows)
        fn = sum(row["fn"] for row in rows)
        unmatched = sum(row["unmatched"] for row in rows)
        precision = tp / kept if kept else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        result.append({
            "minimum_confidence": rows[0]["minimum_confidence"],
            "kept": kept,
            "tp": tp,
            "fn": fn,
            "unmatched": unmatched,
            "precision": _rounded(precision),
            "recall": _rounded(recall),
            "f1": _rounded(f1),
        })
    return result


_PARAMETER_NAMES = (
    "support_weight", "strong_weight", "bilateral_weight",
    "background_caution", "split_bonus", "rescue_bonus",
    "structural_penalty_scale", "bilateral_center",
)


def _family_score(candidate: Mapping[str, Any],
                  parameters: Sequence[float]) -> float:
    (support_weight, strong_weight, bilateral_weight, background_caution,
     split_bonus, rescue_bonus, penalty_scale, bilateral_center) = parameters
    rescued = bool(candidate.get("rescueApplied"))
    sample_fps = float((candidate.get("rescueSampleFps") or 8.0)
                       if rescued else 2.0)
    support_seconds = float(candidate.get("supportFrames") or 0) / sample_fps
    strong_seconds = float(candidate.get("strongFrames") or 0) / sample_fps
    balance = max(0.0, min(1.0, float(
        candidate.get("sideBalance") or 0.0)))
    bilateral = max(0.0, 1.0 - abs(balance - bilateral_center) / 0.35)
    background = max(0.0, min(1.0, (balance - 0.60) / 0.30))
    penalty = max(0.0, float(
        (candidate.get("shortEvidence") or {}).get("penalty") or 0.0))
    score = (0.28
             + support_weight * min(1.0, support_seconds / 5.0)
             + strong_weight * min(1.0, strong_seconds / 3.0)
             + bilateral_weight * bilateral
             - background_caution * background
             + split_bonus * bool(candidate.get("splitApplied"))
             + rescue_bonus * rescued
             - penalty_scale * penalty)
    return max(0.0, min(0.99, score))


def _lobo_robustness(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Tune a small formula family on two scenes and score the held-out third."""
    groups = {}
    for row in rows:
        groups.setdefault(row["benchmark_id"], []).append(row)
    if len(groups) < 3:
        return {"method": "requires at least three benchmark scenes",
                "folds": []}
    grid = itertools.product(
        (0.18, 0.22, 0.26),
        (0.10, 0.14, 0.18),
        (0.12, 0.16, 0.20),
        (0.20, 0.25, 0.30, 0.35),
        (0.08, 0.12, 0.16),
        (0.12, 0.16, 0.20),
        (0.50, 0.70, 0.90),
        (0.30, 0.35, 0.40),
    )
    parameter_grid = list(grid)
    folds = []
    for held_out in groups:
        training_ids = [name for name in groups if name != held_out]
        best = None
        for parameters in parameter_grid:
            scene_aucs = []
            pooled_labels = []
            pooled_scores = []
            for benchmark_id in training_ids:
                training = groups[benchmark_id]
                labels = [bool(row["matched"]) for row in training]
                scores = [_family_score(row["candidate"], parameters)
                          for row in training]
                scene_aucs.append(roc_auc(labels, scores) or 0.0)
                pooled_labels.extend(labels)
                pooled_scores.extend(scores)
            objective = (min(scene_aucs),
                         roc_auc(pooled_labels, pooled_scores) or 0.0,
                         -sum(parameters))
            if best is None or objective > best[0]:
                best = (objective, parameters)
        parameters = best[1]
        held_rows = groups[held_out]
        labels = [bool(row["matched"]) for row in held_rows]
        old_scores = [float(row["raw_confidence"]) for row in held_rows]
        family_scores = [_family_score(row["candidate"], parameters)
                         for row in held_rows]
        folds.append({
            "held_out": held_out,
            "training_benchmarks": training_ids,
            "old_roc_auc": _rounded(roc_auc(labels, old_scores)),
            "family_roc_auc": _rounded(roc_auc(labels, family_scores)),
            "old_pr_auc": _rounded(average_precision(labels, old_scores)),
            "family_pr_auc": _rounded(
                average_precision(labels, family_scores)),
            "old_brier": _rounded(brier_score(labels, old_scores)),
            "family_brier": _rounded(brier_score(labels, family_scores)),
            "parameters": dict(zip(_PARAMETER_NAMES, parameters)),
        })
    return {
        "method": ("choose formula-family coefficients using only two scenes; "
                   "maximize minimum training-scene ROC-AUC, then pooled ROC-AUC"),
        "folds": folds,
        "mean_old_roc_auc": _rounded(statistics.mean(
            fold["old_roc_auc"] for fold in folds)),
        "mean_family_roc_auc": _rounded(statistics.mean(
            fold["family_roc_auc"] for fold in folds)),
    }


def _candidate_rows(manifest_entry: Mapping[str, Any],
                    raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    window = manifest_entry["official_evaluation_window"]
    official = [(source_index, candidate)
                for source_index, candidate in enumerate(raw["candidates"], start=1)
                if overlap_seconds(candidate, window) > 0]
    candidates = [candidate for _, candidate in official]
    matches = maximum_cardinality_overlap_matching(
        manifest_entry["ground_truth"], candidates)
    matched = {prediction_index for _, prediction_index, _ in matches}
    rows = []
    for official_index, (source_index, candidate) in enumerate(official):
        gt_neighbors = [index for index, gt in enumerate(manifest_entry["ground_truth"])
                        if is_primary_match(gt, candidate)]
        near_miss = False
        if not gt_neighbors and manifest_entry["ground_truth"]:
            near_miss = min(interval_gap(candidate, gt)
                            for gt in manifest_entry["ground_truth"]) <= 2.0 + EPSILON
        rows.append({
            "benchmark_id": manifest_entry["benchmark_id"],
            "prediction_index": source_index,
            "official_prediction_index": official_index + 1,
            "matched": official_index in matched,
            "near_miss": near_miss,
            "merge": len(gt_neighbors) >= 2,
            "raw_confidence": float(candidate.get(
                "rawConfidence", candidate.get("confidence", 0.0))),
            "confidence": float(candidate.get("confidence", 0.0)),
            "candidate": candidate,
        })
    return rows


def analyze(manifest: Mapping[str, Any],
            detections: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    all_rows = []
    by_benchmark = {}
    for entry in manifest["benchmarks"]:
        benchmark_id = entry["benchmark_id"]
        rows = _candidate_rows(entry, detections[benchmark_id])
        all_rows.extend(rows)
        by_benchmark[benchmark_id] = {
            "candidate_count": len(rows),
            "matched": sum(bool(row["matched"]) for row in rows),
            "old": _score_summary(rows, "raw_confidence"),
            "new": _score_summary(rows, "confidence"),
            "old_buckets": _calibration_buckets(rows, "raw_confidence"),
            "new_buckets": _calibration_buckets(rows, "confidence"),
            "old_threshold_sweep": _threshold_sweep(
                entry, rows, "raw_confidence"),
            "new_threshold_sweep": _threshold_sweep(
                entry, rows, "confidence"),
            "old_bottom_quartile": _bottom_bucket(rows, "raw_confidence"),
            "new_bottom_quartile": _bottom_bucket(rows, "confidence"),
        }
    return {
        "benchmarks": by_benchmark,
        "aggregate": {
            "candidate_count": len(all_rows),
            "matched": sum(bool(row["matched"]) for row in all_rows),
            "old": _score_summary(all_rows, "raw_confidence"),
            "new": _score_summary(all_rows, "confidence"),
            "old_buckets": _calibration_buckets(all_rows, "raw_confidence"),
            "new_buckets": _calibration_buckets(all_rows, "confidence"),
            "old_bottom_quartile": _bottom_bucket(
                all_rows, "raw_confidence"),
            "new_bottom_quartile": _bottom_bucket(
                all_rows, "confidence"),
            "old_threshold_sweep": _aggregate_sweeps(
                by_benchmark, "old_threshold_sweep"),
            "new_threshold_sweep": _aggregate_sweeps(
                by_benchmark, "new_threshold_sweep"),
        },
        "lobo": _lobo_robustness(all_rows),
        "rows": all_rows,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",
                        default="benchmarks/rally_detection/data/manifest.json")
    parser.add_argument("--benchmark", action="append", nargs=2,
                        metavar=("BENCHMARK_ID", "DETECTION_JSON"), required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text())
    detections = {benchmark_id: json.loads(Path(path).read_text())
                  for benchmark_id, path in args.benchmark}
    report = analyze(manifest, detections)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
