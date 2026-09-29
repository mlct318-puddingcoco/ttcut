import json
import unittest
from pathlib import Path

from benchmarks.rally_detection.evaluate import _subset_differences
from benchmarks.rally_detection.evaluator import (
    evaluate_benchmark,
    evaluate_suite,
    is_primary_match,
    maximum_cardinality_overlap_matching,
)


ROOT = Path(__file__).resolve().parents[1]


def manifest_entry(ground_truth, start=0.0, end=100.0):
    return {
        "benchmark_id": "test",
        "source_duration_seconds": 100.0,
        "official_evaluation_window": {"start": start, "end": end},
        "ground_truth": [
            {"index": index, "start": interval[0], "end": interval[1]}
            for index, interval in enumerate(ground_truth, start=1)
        ],
    }


class CriterionTests(unittest.TestCase):
    def test_half_second_overlap_qualifies(self):
        self.assertTrue(is_primary_match(
            {"start": 0, "end": 3}, {"start": 2.5, "end": 5}))

    def test_quarter_of_shorter_interval_qualifies(self):
        self.assertTrue(is_primary_match(
            {"start": 0, "end": 10}, {"start": 9.7, "end": 10.7}))

    def test_subthreshold_overlap_does_not_qualify(self):
        self.assertFalse(is_primary_match(
            {"start": 0, "end": 10}, {"start": 9.8, "end": 10.8}))


class MatchingTests(unittest.TestCase):
    def test_maximizes_cardinality_before_overlap(self):
        ground_truth = [{"start": 0, "end": 2}, {"start": 2.4, "end": 3.0}]
        predictions = [{"start": 1.5, "end": 2.6}, {"start": 0, "end": 0.6}]
        matches = maximum_cardinality_overlap_matching(ground_truth, predictions)
        self.assertEqual(len(matches), 2)
        self.assertEqual({(g, p) for g, p, _ in matches}, {(0, 1), (1, 0)})

    def test_maximizes_total_overlap_at_equal_cardinality(self):
        ground_truth = [{"start": 0, "end": 4}, {"start": 3, "end": 7}]
        predictions = [{"start": 0, "end": 5}, {"start": 2, "end": 7}]
        matches = maximum_cardinality_overlap_matching(ground_truth, predictions)
        self.assertEqual({(g, p) for g, p, _ in matches}, {(0, 0), (1, 1)})
        self.assertEqual(sum(overlap for _, _, overlap in matches), 8)

    def test_merge_is_reported_from_primary_overlap_graph(self):
        entry = manifest_entry([(0, 2), (2.4, 4.4)])
        result = evaluate_benchmark(entry, [{"start": 0, "end": 4.4}])
        self.assertEqual(result["tp"], 1)
        self.assertEqual(result["nonexclusive_gt_covered"], 2)
        self.assertEqual(result["merge_count"], 1)

    def test_fragmentation_and_extra_fragment_are_reported(self):
        entry = manifest_entry([(0, 4)])
        result = evaluate_benchmark(entry, [
            {"start": 0, "end": 2}, {"start": 2, "end": 4}
        ])
        self.assertEqual(result["tp"], 1)
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["fragmentation_count"], 1)
        self.assertEqual(result["extra_fragment_count"], 1)

    def test_near_miss_excludes_primary_edges_lost_to_a_merge(self):
        entry = manifest_entry([(0, 2), (2.4, 4.4), (8, 9)])
        result = evaluate_benchmark(entry, [
            {"start": 0, "end": 4.4}, {"start": 6.2, "end": 7.2}
        ])
        self.assertEqual(result["near_miss_gt_count"], 1)
        self.assertEqual(result["near_miss_prediction_count"], 1)

    def test_duration_bucket_edges_are_two_and_seven_seconds(self):
        entry = manifest_entry([(0, 2), (10, 16.999), (20, 27)])
        result = evaluate_benchmark(entry, [])
        self.assertEqual(result["duration_buckets"]["short"]["gt"], 1)
        self.assertEqual(result["duration_buckets"]["medium"]["gt"], 1)
        self.assertEqual(result["duration_buckets"]["long"]["gt"], 1)

    def test_official_window_filters_precision_and_counts_nuisance(self):
        entry = manifest_entry([(20, 22)], start=10, end=30)
        result = evaluate_benchmark(entry, [
            {"start": 1, "end": 2}, {"start": 20, "end": 22},
            {"start": 31, "end": 32}
        ])
        self.assertEqual(result["prediction_count"], 1)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["nuisance"]["pre_post_total"], 2)


class FrozenBaselineTests(unittest.TestCase):
    def test_frozen_baseline_reproduces_expected_metrics(self):
        with (ROOT / "data" / "manifest.json").open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        with (ROOT / "data" / "current_koko_predictions.json").open(encoding="utf-8") as handle:
            candidate = json.load(handle)
        with (ROOT / "data" / "frozen_current_koko.json").open(encoding="utf-8") as handle:
            baseline = json.load(handle)
        with (ROOT / "release_gates.json").open(encoding="utf-8") as handle:
            gates = json.load(handle)
        result = evaluate_suite(manifest, candidate, baseline, gates)
        self.assertEqual(_subset_differences(result, baseline["expected_result"]), [])


if __name__ == "__main__":
    unittest.main()
