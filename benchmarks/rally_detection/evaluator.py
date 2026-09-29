"""Deterministic interval evaluation for the Koko rally benchmark suite.

The implementation deliberately uses only the Python standard library so the
frozen regression suite has no relationship to, or dependency on, Huji.
"""

from __future__ import annotations

import math
import re
import statistics
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


OVERLAP_SECONDS = 0.5
SHORTER_INTERVAL_FRACTION = 0.25
NEAR_MISS_SECONDS = 2.0
EPSILON = 1e-9


def _duration(interval: Mapping[str, Any]) -> float:
    return float(interval["end"]) - float(interval["start"])


def _validate_interval(interval: Mapping[str, Any], label: str) -> None:
    if "start" not in interval or "end" not in interval:
        raise ValueError(f"{label} must contain start and end")
    start = float(interval["start"])
    end = float(interval["end"])
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError(f"{label} must have finite values with end > start")


def overlap_seconds(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    return max(0.0, min(float(left["end"]), float(right["end"])) -
               max(float(left["start"]), float(right["start"])))


def is_primary_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    overlap = overlap_seconds(left, right)
    shorter = min(_duration(left), _duration(right))
    return (overlap + EPSILON >= OVERLAP_SECONDS or
            overlap + EPSILON >= SHORTER_INTERVAL_FRACTION * shorter)


def interval_gap(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    if overlap_seconds(left, right) > 0:
        return 0.0
    if float(left["end"]) <= float(right["start"]):
        return float(right["start"]) - float(left["end"])
    return float(left["start"]) - float(right["end"])


class _Edge:
    __slots__ = ("to", "reverse", "capacity", "cost")

    def __init__(self, to: int, reverse: int, capacity: int, cost: int) -> None:
        self.to = to
        self.reverse = reverse
        self.capacity = capacity
        self.cost = cost


def _add_edge(graph: List[List[_Edge]], start: int, end: int,
              capacity: int, cost: int) -> _Edge:
    forward = _Edge(end, len(graph[end]), capacity, cost)
    reverse = _Edge(start, len(graph[start]), 0, -cost)
    graph[start].append(forward)
    graph[end].append(reverse)
    return forward


def maximum_cardinality_overlap_matching(
        ground_truth: Sequence[Mapping[str, Any]],
        predictions: Sequence[Mapping[str, Any]]) -> List[Tuple[int, int, float]]:
    """Maximize match count first, then total overlap, using min-cost max-flow."""
    gt_count = len(ground_truth)
    prediction_count = len(predictions)
    source = 0
    gt_offset = 1
    prediction_offset = gt_offset + gt_count
    sink = prediction_offset + prediction_count
    graph: List[List[_Edge]] = [[] for _ in range(sink + 1)]

    for gt_index in range(gt_count):
        _add_edge(graph, source, gt_offset + gt_index, 1, 0)
    for prediction_index in range(prediction_count):
        _add_edge(graph, prediction_offset + prediction_index, sink, 1, 0)

    match_edges: Dict[Tuple[int, int], Tuple[_Edge, float]] = {}
    for gt_index, gt in enumerate(ground_truth):
        for prediction_index, prediction in enumerate(predictions):
            if not is_primary_match(gt, prediction):
                continue
            overlap = overlap_seconds(gt, prediction)
            cost = -int(round(overlap * 1_000_000))
            edge = _add_edge(graph, gt_offset + gt_index,
                             prediction_offset + prediction_index, 1, cost)
            match_edges[(gt_index, prediction_index)] = (edge, overlap)

    # Bellman-Ford is intentionally used here: the residual graph contains
    # negative costs and the suite is small enough that clarity wins.
    while True:
        distance = [math.inf] * len(graph)
        previous: List[Optional[Tuple[int, int]]] = [None] * len(graph)
        distance[source] = 0
        for _ in range(len(graph) - 1):
            changed = False
            for node, edges in enumerate(graph):
                if math.isinf(distance[node]):
                    continue
                for edge_index, edge in enumerate(edges):
                    if edge.capacity <= 0:
                        continue
                    candidate = distance[node] + edge.cost
                    if candidate < distance[edge.to]:
                        distance[edge.to] = candidate
                        previous[edge.to] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            break
        node = sink
        while node != source:
            prior_node, edge_index = previous[node]  # type: ignore[misc]
            edge = graph[prior_node][edge_index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = prior_node

    matches = [
        (gt_index, prediction_index, overlap)
        for (gt_index, prediction_index), (edge, overlap) in match_edges.items()
        if edge.capacity == 0
    ]
    return sorted(matches)


def percentile(values: Iterable[float], quantile: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rounded(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _mean(values: Sequence[float]) -> Optional[float]:
    return None if not values else sum(values) / len(values)


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _bucket(duration: float) -> str:
    if duration <= 2.0 + EPSILON:
        return "short"
    if duration < 7.0 - EPSILON:
        return "medium"
    return "long"


def _confidence_from_prediction(prediction: Mapping[str, Any]) -> Optional[float]:
    confidence = prediction.get("confidence")
    if confidence is not None:
        value = float(confidence)
        return value / 100.0 if value > 1.0 else value
    for diagnostic in prediction.get("diagnostics", []):
        match = re.search(r"score\s+(\d+)%", str(diagnostic))
        if match:
            return int(match.group(1)) / 100.0
    return None


def normalize_predictions(predictions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for position, prediction in enumerate(predictions, start=1):
        _validate_interval(prediction, f"prediction {position}")
        item: Dict[str, Any] = {
            "index": prediction.get("index", position),
            "start": float(prediction["start"]),
            "end": float(prediction["end"]),
        }
        confidence = _confidence_from_prediction(prediction)
        if confidence is not None:
            item["confidence"] = confidence
        normalized.append(item)
    normalized.sort(key=lambda item: (item["start"], item["end"], str(item["index"])))
    return normalized


def _confidence_summary(
        manifest_entry: Mapping[str, Any],
        predictions: Sequence[Mapping[str, Any]],
        matches: Sequence[Tuple[int, int, float]]) -> Optional[Dict[str, Any]]:
    confidences = [_confidence_from_prediction(prediction) for prediction in predictions]
    if any(value is None for value in confidences):
        return None
    numeric = [float(value) for value in confidences if value is not None]
    matched_indices = {prediction_index for _, prediction_index, _ in matches}
    matched = [value for index, value in enumerate(numeric) if index in matched_indices]
    unmatched = [value for index, value in enumerate(numeric) if index not in matched_indices]

    tiers = [
        ("high_>=0.75", lambda value: value >= 0.75),
        ("mid_0.55_to_0.74", lambda value: 0.55 <= value < 0.75),
        ("low_<0.55", lambda value: value < 0.55),
    ]
    tier_rows = []
    for name, predicate in tiers:
        indices = [index for index, value in enumerate(numeric) if predicate(value)]
        tier_matched = sum(index in matched_indices for index in indices)
        tier_rows.append({
            "tier": name,
            "prediction_count": len(indices),
            "matched": tier_matched,
            "unmatched": len(indices) - tier_matched,
            "precision": (tier_matched / len(indices)) if indices else None,
        })

    threshold_rows = []
    for threshold in (0.0, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.9):
        retained = [prediction for prediction, confidence in zip(predictions, numeric)
                    if confidence + EPSILON >= threshold]
        result = evaluate_benchmark(manifest_entry, retained, include_confidence=False)
        threshold_rows.append({
            "minimum_confidence": threshold,
            "kept": len(retained),
            "tp": result["tp"],
            "fn": result["fn"],
            "unmatched": result["unmatched"],
            "precision": result["precision"],
            "recall": result["recall"],
            "f1": result["f1"],
        })

    return {
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "matched_mean": _rounded(_mean(matched)),
        "matched_median": _rounded(statistics.median(matched) if matched else None),
        "unmatched_mean": _rounded(_mean(unmatched)),
        "unmatched_median": _rounded(statistics.median(unmatched) if unmatched else None),
        "tiers": tier_rows,
        "threshold_sweep": threshold_rows,
    }


def evaluate_benchmark(manifest_entry: Mapping[str, Any],
                       raw_predictions: Sequence[Mapping[str, Any]],
                       runtime_seconds: Optional[float] = None,
                       coverage: str = "full_source",
                       include_confidence: bool = True) -> Dict[str, Any]:
    """Evaluate predictions for one benchmark using the frozen methodology."""
    ground_truth = deepcopy(list(manifest_entry["ground_truth"]))
    for position, interval in enumerate(ground_truth, start=1):
        _validate_interval(interval, f"ground truth {position}")
    all_predictions = normalize_predictions(raw_predictions)
    window = manifest_entry["official_evaluation_window"]
    official_predictions = [
        prediction for prediction in all_predictions
        if overlap_seconds(prediction, window) > 0
    ]
    matches = maximum_cardinality_overlap_matching(ground_truth, official_predictions)
    matched_gt = {gt_index for gt_index, _, _ in matches}
    matched_predictions = {prediction_index for _, prediction_index, _ in matches}

    gt_neighbors: List[List[int]] = [[] for _ in ground_truth]
    prediction_neighbors: List[List[int]] = [[] for _ in official_predictions]
    for gt_index, gt in enumerate(ground_truth):
        for prediction_index, prediction in enumerate(official_predictions):
            if is_primary_match(gt, prediction):
                gt_neighbors[gt_index].append(prediction_index)
                prediction_neighbors[prediction_index].append(gt_index)

    pairs = []
    start_abs_errors = []
    end_abs_errors = []
    for gt_index, prediction_index, overlap in matches:
        gt = ground_truth[gt_index]
        prediction = official_predictions[prediction_index]
        start_error = float(prediction["start"]) - float(gt["start"])
        end_error = float(prediction["end"]) - float(gt["end"])
        start_abs_errors.append(abs(start_error))
        end_abs_errors.append(abs(end_error))
        pairs.append({
            "gt_index": gt.get("index", gt_index + 1),
            "prediction_index": prediction.get("index", prediction_index + 1),
            "overlap": _rounded(overlap),
            "start_error": _rounded(start_error),
            "end_error": _rounded(end_error),
        })

    tp = len(matches)
    fn = len(ground_truth) - tp
    unmatched = len(official_predictions) - tp
    precision = tp / len(official_predictions) if official_predictions else 0.0
    recall = tp / len(ground_truth) if ground_truth else 0.0

    duration_buckets: Dict[str, Dict[str, Any]] = {}
    for bucket_name in ("short", "medium", "long"):
        indices = [index for index, gt in enumerate(ground_truth)
                   if _bucket(_duration(gt)) == bucket_name]
        matched_count = sum(index in matched_gt for index in indices)
        covered_count = sum(bool(gt_neighbors[index]) for index in indices)
        count = len(indices)
        duration_buckets[bucket_name] = {
            "gt": count,
            "tp": matched_count,
            "recall": matched_count / count if count else 0.0,
            "nonexclusive_covered": covered_count,
            "nonexclusive_recall": covered_count / count if count else 0.0,
        }

    fragmentations = [
        {
            "gt_index": ground_truth[index].get("index", index + 1),
            "prediction_indices": [official_predictions[p].get("index", p + 1)
                                   for p in neighbors],
        }
        for index, neighbors in enumerate(gt_neighbors) if len(neighbors) >= 2
    ]
    merges = [
        {
            "prediction_index": official_predictions[index].get("index", index + 1),
            "gt_indices": [ground_truth[g].get("index", g + 1) for g in neighbors],
        }
        for index, neighbors in enumerate(prediction_neighbors) if len(neighbors) >= 2
    ]

    near_miss_gt = []
    for gt_index, gt in enumerate(ground_truth):
        if gt_index in matched_gt or gt_neighbors[gt_index] or not official_predictions:
            continue
        gaps = [(interval_gap(gt, prediction), prediction_index)
                for prediction_index, prediction in enumerate(official_predictions)]
        gap, prediction_index = min(gaps)
        if gap <= NEAR_MISS_SECONDS + EPSILON:
            near_miss_gt.append({
                "gt_index": gt.get("index", gt_index + 1),
                "prediction_index": official_predictions[prediction_index].get(
                    "index", prediction_index + 1),
                "gap": _rounded(gap),
            })

    near_miss_predictions = []
    for prediction_index, prediction in enumerate(official_predictions):
        if (prediction_index in matched_predictions or
                prediction_neighbors[prediction_index] or not ground_truth):
            continue
        gaps = [(interval_gap(prediction, gt), gt_index)
                for gt_index, gt in enumerate(ground_truth)]
        gap, gt_index = min(gaps)
        if gap <= NEAR_MISS_SECONDS + EPSILON:
            near_miss_predictions.append({
                "prediction_index": prediction.get("index", prediction_index + 1),
                "gt_index": ground_truth[gt_index].get("index", gt_index + 1),
                "gap": _rounded(gap),
            })

    result: Dict[str, Any] = {
        "benchmark_id": manifest_entry["benchmark_id"],
        "gt_count": len(ground_truth),
        "prediction_count": len(official_predictions),
        "tp": tp,
        "fn": fn,
        "unmatched": unmatched,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "nonexclusive_gt_covered": sum(bool(neighbors) for neighbors in gt_neighbors),
        "nonexclusive_recall": (sum(bool(neighbors) for neighbors in gt_neighbors) /
                                len(ground_truth) if ground_truth else 0.0),
        "duration_buckets": duration_buckets,
        "fragmentation_count": len(fragmentations),
        "extra_fragment_count": sum(len(item["prediction_indices"]) - 1
                                    for item in fragmentations),
        "merge_count": len(merges),
        "near_miss_gt_count": len(near_miss_gt),
        "near_miss_prediction_count": len(near_miss_predictions),
        "boundary": {
            "start_abs_median": _rounded(percentile(start_abs_errors, 0.5)),
            "start_abs_p90": _rounded(percentile(start_abs_errors, 0.9)),
            "end_abs_median": _rounded(percentile(end_abs_errors, 0.5)),
            "end_abs_p90": _rounded(percentile(end_abs_errors, 0.9)),
        },
        "pairs": pairs,
        "fragmentations": fragmentations,
        "merges": merges,
        "near_miss_gt": near_miss_gt,
        "near_miss_predictions": near_miss_predictions,
        "runtime_seconds": None if runtime_seconds is None else float(runtime_seconds),
        "realtime_factor": (None if runtime_seconds is None else
                            float(runtime_seconds) /
                            float(manifest_entry["source_duration_seconds"])),
        "coverage": coverage,
        "nuisance": None,
    }
    if coverage == "full_source":
        before = sum(float(prediction["end"]) <= float(window["start"]) + EPSILON
                     for prediction in all_predictions)
        after = sum(float(prediction["start"]) >= float(window["end"]) - EPSILON
                    for prediction in all_predictions)
        result["nuisance"] = {
            "pre_match": before,
            "post_match": after,
            "pre_post_total": before + after,
        }
    if include_confidence:
        result["confidence"] = _confidence_summary(
            manifest_entry, official_predictions, matches)
    return result

def aggregate_results(per_benchmark: Mapping[str, Mapping[str, Any]],
                      manifest: Mapping[str, Any]) -> Dict[str, Any]:
    ordered = [per_benchmark[entry["benchmark_id"]]
               for entry in manifest["benchmarks"]]
    total_gt = sum(result["gt_count"] for result in ordered)
    total_predictions = sum(result["prediction_count"] for result in ordered)
    total_tp = sum(result["tp"] for result in ordered)
    total_fn = sum(result["fn"] for result in ordered)
    total_unmatched = sum(result["unmatched"] for result in ordered)
    micro_precision = total_tp / total_predictions if total_predictions else 0.0
    micro_recall = total_tp / total_gt if total_gt else 0.0

    bucket_totals = {}
    for bucket in ("short", "medium", "long"):
        gt_count = sum(result["duration_buckets"][bucket]["gt"] for result in ordered)
        tp = sum(result["duration_buckets"][bucket]["tp"] for result in ordered)
        bucket_totals[bucket] = {
            "gt": gt_count,
            "tp": tp,
            "recall": tp / gt_count if gt_count else 0.0,
        }

    runtimes = [result["runtime_seconds"] for result in ordered]
    weighted_realtime_factor = None
    runtime_seconds = None
    if all(value is not None for value in runtimes):
        runtime_seconds = sum(float(value) for value in runtimes)
        source_seconds = sum(float(entry["source_duration_seconds"])
                             for entry in manifest["benchmarks"])
        weighted_realtime_factor = runtime_seconds / source_seconds

    nuisance_02_03 = None
    nuisance_results = [per_benchmark.get("benchmark_02"),
                        per_benchmark.get("benchmark_03")]
    if all(result and result.get("nuisance") is not None for result in nuisance_results):
        nuisance_02_03 = sum(result["nuisance"]["pre_post_total"]
                            for result in nuisance_results if result)

    return {
        "gt_count": total_gt,
        "prediction_count": total_predictions,
        "tp": total_tp,
        "fn": total_fn,
        "unmatched": total_unmatched,
        "macro": {
            "precision": _mean([result["precision"] for result in ordered]),
            "recall": _mean([result["recall"] for result in ordered]),
            "f1": _mean([result["f1"] for result in ordered]),
        },
        "micro": {
            "precision": micro_precision,
            "recall": micro_recall,
            "f1": _f1(micro_precision, micro_recall),
        },
        "duration_buckets": bucket_totals,
        "fragmentation_count": sum(result["fragmentation_count"] for result in ordered),
        "extra_fragment_count": sum(result["extra_fragment_count"] for result in ordered),
        "merge_count": sum(result["merge_count"] for result in ordered),
        "near_miss_gt_count": sum(result["near_miss_gt_count"] for result in ordered),
        "near_miss_prediction_count": sum(
            result["near_miss_prediction_count"] for result in ordered),
        "runtime_seconds": runtime_seconds,
        "weighted_realtime_factor": weighted_realtime_factor,
        "benchmark_02_03_pre_post_nuisance": nuisance_02_03,
    }


def _gate(name: str, actual: Optional[float], target: float,
          comparator: str) -> Dict[str, Any]:
    if actual is None:
        return {"name": name, "status": "not_evaluated", "actual": None,
                "target": target, "comparator": comparator}
    passed = actual + EPSILON >= target if comparator == ">=" else actual <= target + EPSILON
    return {"name": name, "status": "pass" if passed else "fail",
            "actual": actual, "target": target, "comparator": comparator}


def evaluate_release_gates(result: Mapping[str, Any], baseline: Mapping[str, Any],
                           gates: Mapping[str, Any]) -> Dict[str, Any]:
    aggregate = result["aggregate"]
    baseline_aggregate = baseline["expected_result"]["aggregate"]
    rows = []
    for benchmark_id, benchmark_result in result["per_benchmark"].items():
        target = (baseline["expected_result"]["per_benchmark"][benchmark_id]["recall"] -
                  gates["per_match_recall_regression_max_points"] / 100.0)
        rows.append(_gate(f"{benchmark_id}_recall", benchmark_result["recall"],
                          target, ">="))
    rows.extend([
        _gate("macro_recall", aggregate["macro"]["recall"],
              baseline_aggregate["macro"]["recall"] +
              gates["macro_recall_improvement_points"] / 100.0, ">="),
        _gate("micro_recall", aggregate["micro"]["recall"],
              baseline_aggregate["micro"]["recall"] +
              gates["micro_recall_improvement_points"] / 100.0, ">="),
        _gate("total_tp", aggregate["tp"],
              baseline_aggregate["tp"] + gates["total_tp_improvement"], ">="),
        _gate("aggregated_short_recall", aggregate["duration_buckets"]["short"]["recall"],
              gates["aggregated_short_recall_min"], ">="),
        _gate("macro_f1", aggregate["macro"]["f1"],
              baseline_aggregate["macro"]["f1"] +
              gates["macro_f1_improvement_points"] / 100.0, ">="),
        _gate("micro_f1", aggregate["micro"]["f1"],
              baseline_aggregate["micro"]["f1"] +
              gates["micro_f1_improvement_points"] / 100.0, ">="),
        _gate("total_merges", aggregate["merge_count"], gates["total_merges_max"], "<="),
        _gate("weighted_realtime_factor", aggregate["weighted_realtime_factor"],
              gates["weighted_realtime_factor_max"], "<="),
        _gate("official_unmatched_total", aggregate["unmatched"],
              gates["official_unmatched_total_max"], "<="),
        _gate("benchmark_02_03_pre_post_nuisance",
              aggregate["benchmark_02_03_pre_post_nuisance"],
              gates["benchmark_02_03_pre_post_nuisance_max"], "<="),
    ])
    return {
        "complete": all(row["status"] != "not_evaluated" for row in rows),
        "all_passed": all(row["status"] == "pass" for row in rows),
        "gates": rows,
    }


def evaluate_suite(manifest: Mapping[str, Any], candidate: Mapping[str, Any],
                   baseline: Optional[Mapping[str, Any]] = None,
                   gates: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    candidate_benchmarks = candidate.get("benchmarks", {})
    per_benchmark: Dict[str, Any] = {}
    for entry in manifest["benchmarks"]:
        benchmark_id = entry["benchmark_id"]
        if benchmark_id not in candidate_benchmarks:
            raise ValueError(f"candidate is missing {benchmark_id}")
        supplied = candidate_benchmarks[benchmark_id]
        predictions = supplied.get("predictions", supplied.get("candidates"))
        if predictions is None:
            raise ValueError(f"{benchmark_id} must contain predictions or candidates")
        per_benchmark[benchmark_id] = evaluate_benchmark(
            entry, predictions,
            runtime_seconds=supplied.get("runtime_seconds"),
            coverage=supplied.get("coverage", "full_source"),
        )
    result: Dict[str, Any] = {
        "schema_version": 1,
        "detector": candidate.get("detector", "candidate"),
        "methodology": manifest["methodology"],
        "per_benchmark": per_benchmark,
        "aggregate": aggregate_results(per_benchmark, manifest),
    }
    if baseline is not None and gates is not None:
        result["release_gates"] = evaluate_release_gates(result, baseline, gates)
    return result
