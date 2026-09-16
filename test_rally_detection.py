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
            result.append(dict(t=i / 2, motion=8.0 if active else .15,
                               left=7.0 if active else .1,
                               right=9.0 if active else .2))
        return result

    def test_visual_episodes_create_separate_candidates(self):
        config = DetectorConfig()
        candidates, diagnostics = analyze_motion(
            self._metrics([(16, 27), (48, 59)]), [8.1, 8.6, 24.2], config, 80)
        self.assertEqual(len(candidates), 2)
        self.assertLess(candidates[0]["end"], candidates[1]["start"])
        self.assertGreater(candidates[0]["audioHits"], 0)
        self.assertGreater(diagnostics["motionThreshold"], diagnostics["motionBaseline"])

    def test_audio_alone_never_creates_a_candidate(self):
        candidates, _ = analyze_motion(self._metrics([]), [2.0, 2.5, 3.0],
                                       DetectorConfig(), 20)
        self.assertEqual(candidates, [])

    def test_audio_gap_does_not_split_a_visual_group(self):
        metrics = self._metrics([(20, 47)])
        impacts = [10.5, 11.0, 11.5, 13.0, 13.5]
        candidates, diagnostics = analyze_motion(metrics, impacts, DetectorConfig(), 20)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["boundaryBasis"], "motion")
        self.assertEqual(diagnostics["audioAssistedSplits"], 0)

    def test_audio_gap_can_trim_a_short_tail_but_not_add_a_candidate(self):
        metrics = self._metrics([(20, 31)])
        impacts = [10.1, 10.6, 11.1, 11.6, 12.1, 12.6, 13.1,
                   14.6, 15.1, 15.4]
        candidates, diagnostics = analyze_motion(metrics, impacts,
                                                 DetectorConfig(), 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["boundaryBasis"],
                         "motion+audio-trim-tail")
        self.assertEqual(diagnostics["audioTrimmedCandidates"], 1)

    def test_audio_can_only_promote_sustained_near_threshold_motion(self):
        metrics = self._metrics([])
        for index, motion in zip(range(20, 23), (1.0, 2.0, 1.0)):
            metrics[index].update(motion=motion, left=motion, right=motion * .8)
        impacts = [10.1, 10.6, 11.1]
        candidates, diagnostics = analyze_motion(metrics, impacts, DetectorConfig(), 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["boundaryBasis"], "motion+audio-support")
        self.assertEqual(candidates[0]["strongFrames"], 1)
        self.assertEqual(diagnostics["audioPromotedCandidates"], 1)

    def test_audio_cannot_promote_motion_without_a_main_threshold_seed(self):
        metrics = self._metrics([])
        for index in range(20, 23):
            metrics[index].update(motion=.9, left=.9, right=.8)
        candidates, _ = analyze_motion(metrics, [10.1, 10.6, 11.1],
                                       DetectorConfig(), 80)
        self.assertEqual(candidates, [])

    def test_brief_one_sided_low_audio_motion_is_rejected(self):
        metrics = self._metrics([(20, 20)])
        metrics[20]["right"] = .75
        candidates, diagnostics = analyze_motion(metrics, [10.5],
                                                  DetectorConfig(), 80)
        self.assertEqual(candidates, [])
        self.assertEqual(diagnostics["rejectedBriefCandidates"], 1)

    def _joined_rallies(self, valleys, valley_motion=1.5):
        metrics = self._metrics([(20, 56)])
        for first, last in valleys:
            for item in metrics[first:last + 1]:
                item.update(motion=valley_motion, left=valley_motion,
                            right=valley_motion * .8)
        return metrics

    def test_two_sustained_motion_valleys_split_three_rallies(self):
        metrics = self._joined_rallies([(30, 31), (44, 45)])
        candidates, diagnostics = analyze_motion(metrics, [],
                                                 DetectorConfig(), 80)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(diagnostics["motionValleySplits"], 2)
        self.assertTrue(all(c["splitDecision"] == "split" for c in candidates))
        self.assertLess(candidates[0]["end"], candidates[1]["start"])
        self.assertLess(candidates[1]["end"], candidates[2]["start"])
        self.assertTrue(all(c["motionValleyDuration"] >= 1.0
                            for c in candidates))

    def test_one_frame_motion_dip_does_not_split(self):
        metrics = self._joined_rallies([(35, 35)])
        candidates, diagnostics = analyze_motion(metrics, [],
                                                 DetectorConfig(), 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(diagnostics["motionValleySplits"], 0)

    def test_audio_vetoes_shallow_short_pause(self):
        config = DetectorConfig(min_threshold=5.0)
        metrics = self._joined_rallies([(35, 36)], valley_motion=4.0)
        candidates, diagnostics = analyze_motion(metrics, [17.6, 18.0],
                                                 config, 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(diagnostics["motionValleySplits"], 0)
        self.assertEqual(candidates[0]["splitReason"],
                         "audio_continues_during_pause")

    def test_no_visual_restart_does_not_split(self):
        config = DetectorConfig(min_threshold=5.0)
        metrics = self._joined_rallies([(35, 36)], valley_motion=1.5)
        for item in metrics[37:57]:
            item.update(motion=5.0, left=5.0, right=4.0)
        candidates, diagnostics = analyze_motion(metrics, [], config, 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(diagnostics["motionValleySplits"], 0)
        self.assertEqual(candidates[0]["splitReason"], "no_visual_restart")

    def test_split_cannot_create_one_sided_child(self):
        metrics = self._joined_rallies([(35, 36)])
        for item in metrics[37:57]:
            item["right"] = .01
        candidates, diagnostics = analyze_motion(metrics, [],
                                                 DetectorConfig(), 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(diagnostics["motionValleySplits"], 0)
        self.assertEqual(candidates[0]["splitReason"], "fragment_guard")

    def test_one_long_candidate_is_limited_to_three_pieces(self):
        metrics = self._metrics([(20, 80)])
        for first in (31, 46, 61):
            for item in metrics[first:first + 2]:
                item.update(motion=1.5, left=1.5, right=1.2)
        candidates, diagnostics = analyze_motion(metrics, [],
                                                 DetectorConfig(), 80)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(diagnostics["motionValleySplits"], 2)
        self.assertTrue(any(check["reason"] == "split_limit"
                            for check in candidates[0]["splitChecks"]))

    def test_one_sided_motion_is_rejected(self):
        metrics = self._metrics([(20, 35)])
        for item in metrics[20:36]:
            item["right"] = .01
        candidates, _ = analyze_motion(metrics, [], DetectorConfig(), 20)
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
