import unittest

from rally_detection import (
    DetectionError,
    DetectorConfig,
    _motion_between,
    analyze_motion,
    percentile,
    validate_roi,
)


class PercentileTests(unittest.TestCase):
    def test_interpolates(self):
        self.assertEqual(percentile([0, 10], 0.25), 2.5)


class RoiTests(unittest.TestCase):
    def test_accepts_normalized_roi(self):
        self.assertEqual(validate_roi({"x": .1, "y": .2, "w": .7, "h": .6}),
                         {"x": .1, "y": .2, "w": .7, "h": .6})

    def test_rejects_tiny_or_out_of_bounds_roi(self):
        with self.assertRaises(DetectionError):
            validate_roi({"x": .2, "y": .2, "w": .02, "h": .5})
        with self.assertRaises(DetectionError):
            validate_roi({"x": .8, "y": .2, "w": .4, "h": .5})


class MotionTests(unittest.TestCase):
    def test_motion_is_split_into_left_and_right_halves(self):
        before = bytes([0] * 8)
        after = bytes([20, 0, 30, 0, 20, 0, 30, 0])
        whole, left, right = _motion_between(before, after, 4, 12)
        self.assertEqual(whole, 12.5)
        self.assertEqual(left, 10.0)
        self.assertEqual(right, 15.0)

    def _metrics(self, episodes):
        result = []
        for i in range(160):
            active = any(start <= i <= end for start, end in episodes)
            result.append(dict(t=i / 8, motion=8.0 if active else .15,
                               left=7.0 if active else .1,
                               right=9.0 if active else .2))
        return result

    def test_visual_episodes_create_separate_candidates(self):
        config = DetectorConfig()
        candidates, diagnostics = analyze_motion(
            self._metrics([(16, 27), (48, 59)]), [2.1, 2.6, 6.4], config, 20)
        self.assertEqual(len(candidates), 2)
        self.assertLess(candidates[0]["end"], candidates[1]["start"])
        self.assertGreater(candidates[0]["audioHits"], 0)
        self.assertGreater(diagnostics["motionThreshold"], diagnostics["motionBaseline"])

    def test_audio_alone_never_creates_a_candidate(self):
        candidates, _ = analyze_motion(self._metrics([]), [2.0, 2.5, 3.0],
                                       DetectorConfig(), 20)
        self.assertEqual(candidates, [])

    def test_audio_gap_only_splits_an_existing_visual_group(self):
        metrics = self._metrics([(20, 47)])
        impacts = [2.55, 2.8, 3.05, 4.35, 4.6]
        candidates, diagnostics = analyze_motion(metrics, impacts, DetectorConfig(), 20)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["boundaryBasis"], "motion+audio-gap")
        self.assertEqual(diagnostics["audioAssistedSplits"], 1)

    def test_one_sided_motion_is_rejected(self):
        metrics = self._metrics([(20, 35)])
        for item in metrics[20:36]:
            item["right"] = .01
        candidates, _ = analyze_motion(metrics, [], DetectorConfig(), 20)
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
