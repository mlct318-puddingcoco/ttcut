import unittest

from benchmarks.rally_detection.analyze_confidence import (
    analyze,
    average_precision,
    brier_score,
    roc_auc,
)


class ConfidenceMetricTests(unittest.TestCase):
    def test_rank_and_calibration_metrics(self):
        labels = [True, False, True, False]
        perfect = [.9, .2, .8, .1]
        self.assertEqual(roc_auc(labels, perfect), 1.0)
        self.assertEqual(average_precision(labels, perfect), 1.0)
        self.assertAlmostEqual(brier_score(labels, perfect), .025)

    def test_average_precision_is_tie_order_independent(self):
        self.assertEqual(
            average_precision([True, False, True], [.8, .8, .2]),
            average_precision([False, True, True], [.8, .8, .2]))

    def test_analysis_uses_official_one_to_one_labels_and_raw_score(self):
        manifest = {"benchmarks": [{
            "benchmark_id": "example",
            "official_evaluation_window": {"start": 0.0, "end": 20.0},
            "ground_truth": [
                {"index": 1, "start": 2.0, "end": 4.0},
                {"index": 2, "start": 8.0, "end": 10.0},
            ],
        }]}
        detections = {"example": {"candidates": [
            {"start": 1.8, "end": 4.1, "rawConfidence": .4,
             "confidence": .8},
            {"start": 2.2, "end": 3.8, "rawConfidence": .9,
             "confidence": .3},
            {"start": 8.0, "end": 10.0, "rawConfidence": .5,
             "confidence": .9},
            {"start": 30.0, "end": 31.0, "rawConfidence": .7,
             "confidence": .7},
        ]}}
        report = analyze(manifest, detections)
        rows = report["rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["matched"] for row in rows), 2)
        self.assertEqual(rows[0]["raw_confidence"], .4)
        self.assertEqual(rows[0]["confidence"], .8)
        self.assertEqual(report["aggregate"]["candidate_count"], 3)


if __name__ == "__main__":
    unittest.main()
