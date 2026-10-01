import unittest

from rally_detection import (
    DetectionError,
    DetectorConfig,
    _motion_between,
    _split_on_motion_valleys,
    analyze_rescue_window,
    analyze_rescue_window_candidates,
    analyze_motion,
    integrate_rescue_candidate,
    percentile,
    propose_rescue_windows,
    refine_candidate_end,
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

    def test_candidate_duration_does_not_replace_video_duration(self):
        candidates, _ = analyze_motion(
            self._metrics([(16, 27), (48, 59)]), [], DetectorConfig())
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(candidate["end"] > candidate["start"]
                            for candidate in candidates))
        self.assertGreater(candidates[1]["start"], candidates[0]["end"])

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
        brief = [check for check in candidates[0]["splitChecks"]
                 if check["reason"] == "brief_valley"]
        self.assertTrue(brief)
        self.assertIsNotNone(brief[0]["valleyDepth"])

    def test_audio_lowers_confidence_of_shallow_short_pause(self):
        config = DetectorConfig(min_threshold=5.0)
        metrics = self._joined_rallies([(35, 36)], valley_motion=4.0)
        candidates, diagnostics = analyze_motion(metrics, [17.6, 18.0],
                                                 config, 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(diagnostics["motionValleySplits"], 0)
        self.assertEqual(candidates[0]["splitReason"],
                         "low_split_confidence")
        check = next(check for check in candidates[0]["splitChecks"]
                     if check["reason"] == "low_split_confidence")
        self.assertGreater(check["audioPenalty"], 0)
        self.assertLess(check["splitConfidence"], check["visualConfidence"])

    def test_sustained_visual_valley_survives_background_audio(self):
        config = DetectorConfig(min_threshold=5.0)
        metrics = self._joined_rallies([(35, 37)], valley_motion=4.0)
        candidates, diagnostics = analyze_motion(metrics, [17.6, 18.0],
                                                 config, 80)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(diagnostics["motionValleySplits"], 1)
        check = next(check for check in candidates[0]["splitChecks"]
                     if check["decision"] == "split")
        self.assertGreater(check["audioPenalty"], 0)
        self.assertGreaterEqual(check["splitConfidence"],
                                config.split_confidence_threshold)

    def test_time_order_considers_earlier_valleys_before_deeper_later_one(self):
        metrics = self._metrics([(20, 80)])
        for first, motion in ((30, 1.8), (47, 1.5), (64, 1.2)):
            for item in metrics[first:first + 2]:
                item.update(motion=motion, left=motion, right=motion * .8)
        candidates, diagnostics = analyze_motion(metrics, [],
                                                 DetectorConfig(), 80)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(diagnostics["motionValleySplits"], 2)
        split_points = sorted({c["splitPoint"] for c in candidates})
        self.assertLess(split_points[-1], 30.0)
        self.assertTrue(any(check["reason"] == "split_limit"
                            for check in candidates[0]["splitChecks"]))

    def test_weak_short_candidate_is_retained_but_low_confidence(self):
        metrics = self._metrics([])
        for item in metrics[20:23]:
            item.update(motion=6.0, left=6.0, right=1.2)
        candidates, _ = analyze_motion(metrics, [10.2, 10.6, 11.0],
                                       DetectorConfig(min_threshold=5.0), 80)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["confidenceTier"], "low")
        self.assertGreater(candidates[0]["shortEvidence"]["penalty"], 0)
        self.assertLess(candidates[0]["confidence"],
                        candidates[0]["baseConfidence"])

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

    def test_long_no_audio_valley_with_strong_restart_uses_anti_merge_guard(self):
        config = DetectorConfig(min_threshold=5.0)
        metrics = []
        raw = [6.0] * 60
        for index in range(20, 23):
            raw[index] = 4.2
        for index in range(23, 29):
            raw[index] = 20.0
        for index, motion in enumerate(raw):
            metrics.append(dict(t=index / 2, motion=motion,
                                left=motion, right=motion * .9))
        parts, checks = _split_on_motion_valleys(
            list(range(60)), metrics, raw, raw, [], 5.0, config)
        self.assertEqual(len(parts), 2)
        split = next(check for check in checks if check["decision"] == "split")
        self.assertTrue(split["antiMergeGuard"])
        self.assertEqual(split["reason"],
                         "long_candidate_clear_gap_visual_restart")

    def test_one_sided_motion_is_rejected(self):
        metrics = self._metrics([(20, 35)])
        for item in metrics[20:36]:
            item["right"] = .01
        candidates, _ = analyze_motion(metrics, [], DetectorConfig(), 20)
        self.assertEqual(candidates, [])


class LocalRescueAndEndRefinementTests(unittest.TestCase):
    def _coarse(self, active=None, one_sided=False):
        result = []
        for index in range(40):
            motion = 2.4 if index == active else .15
            left = motion if index == active else .1
            right = (.12 if one_sided and index == active else
                     motion * .9 if index == active else .2)
            result.append(dict(t=index / 2, motion=motion,
                               left=left, right=right))
        return result

    def _dense_burst(self, start=9.25, active=range(6, 15), one_sided=False):
        result = []
        for index in range(22):
            on = index in active
            motion = 4.0 if on else .12
            result.append(dict(
                t=start + index / 8, motion=motion,
                left=3.6 if on else .1,
                right=(.10 if one_sided and on else 4.2 if on else .12)))
        return result

    def _dense_two_bursts(self, start=9.0):
        result = []
        for index in range(32):
            on = index in range(3, 11) or index in range(19, 27)
            motion = 4.2 if on else .08
            result.append(dict(
                t=start + index / 8, motion=motion,
                left=3.8 if on else .05,
                right=4.4 if on else .06))
        return result

    def test_sub_two_second_rally_missed_by_coarse_is_rescued_locally(self):
        config = DetectorConfig()
        impacts = [10.05, 10.42, 10.78]
        windows = propose_rescue_windows(
            self._coarse(active=21), impacts, [], config, 20)
        self.assertEqual(len(windows), 1)
        candidate = analyze_rescue_window(
            self._dense_burst(), impacts, windows[0], config, 20)
        self.assertIsNotNone(candidate)
        self.assertLessEqual(candidate["duration"], 2.0)
        self.assertTrue(candidate["rescueApplied"])
        self.assertEqual(candidate["boundaryBasis"],
                         "local-motion+audio-rescue")

    def test_nearby_background_motion_is_not_a_rescue_candidate(self):
        config = DetectorConfig()
        impacts = [10.05, 10.42, 10.78]
        windows = propose_rescue_windows(
            self._coarse(active=21, one_sided=True), impacts, [], config, 20)
        self.assertEqual(windows, [])
        proposal = dict(start=9.25, end=12.0, coreStart=10.0, coreEnd=10.9)
        self.assertIsNone(analyze_rescue_window(
            self._dense_burst(one_sided=True), impacts, proposal, config, 20))

    def test_strong_audio_and_brief_one_sided_coarse_motion_reaches_dense_gate(self):
        config = DetectorConfig()
        coarse = self._coarse(active=21)
        coarse[21]["right"] = .24
        impacts = [10.0, 10.28, 10.56, 10.84, 11.12]
        windows = propose_rescue_windows(coarse, impacts, [], config, 20)
        self.assertEqual(len(windows), 1)
        self.assertEqual(
            windows[0]["rescueTrigger"],
            "strong-audio+brief-one-sided-coarse-motion")
        candidate = analyze_rescue_window(
            self._dense_burst(), impacts, windows[0], config, 20)
        self.assertIsNotNone(candidate)
        self.assertIn("brief-one-sided", candidate["rescueBasis"])

    def test_isolated_audio_without_visual_evidence_is_rejected(self):
        impacts = [10.0, 10.28, 10.56, 10.84, 11.12]
        windows, diagnostics = propose_rescue_windows(
            self._coarse(), impacts, [], DetectorConfig(), 20,
            include_diagnostics=True)
        self.assertEqual(windows, [])
        self.assertGreater(
            diagnostics["rejections"].get("coarse_motion_below_floor", 0), 0)

    def test_two_dense_bursts_separated_by_real_valley_remain_separate(self):
        proposal = dict(
            start=9.0, end=13.0, coreStart=9.2, coreEnd=12.8,
            rescueTrigger="audio-cluster+bilateral-coarse-motion",
            minDenseAudioHits=3)
        impacts = [9.5, 9.85, 10.15, 11.55, 11.9, 12.2]
        candidates = analyze_rescue_window_candidates(
            self._dense_two_bursts(), impacts, proposal,
            DetectorConfig(), 20)
        self.assertEqual(len(candidates), 2)
        self.assertLess(candidates[0]["end"], candidates[1]["start"])
        self.assertTrue(all(
            item["rescueValleyGuard"]["decision"]
            == "kept_as_separate_burst" for item in candidates))

    def test_rescue_does_not_fuse_neighbor_without_visual_continuity(self):
        rescue = dict(
            start=9.7, end=11.0, duration=1.3,
            visualStart=10.2, visualEnd=10.8,
            rescueUnionDecision=None)
        existing = [dict(
            start=8.0, end=10.0, visualStart=8.2, visualEnd=9.4)]
        integrated, decision = integrate_rescue_candidate(
            rescue, existing, DetectorConfig())
        self.assertIsNotNone(integrated)
        self.assertGreaterEqual(integrated["start"], existing[0]["end"])
        self.assertEqual(decision, "kept_separate_at_dense_valley")

    def test_end_refinement_shortens_overextended_candidate(self):
        config = DetectorConfig()
        candidate = dict(start=10.0, end=15.0, duration=5.0)
        dense = self._dense_burst(start=11.5, active=range(0, 13))
        refined = refine_candidate_end(
            candidate, dense, [12.0, 12.45, 12.88], config)
        self.assertTrue(refined["refinedEnd"])
        self.assertLess(refined["end"], candidate["end"])
        self.assertEqual(refined["endRefineBasis"],
                         "dense-motion-fall+audio-quiet")

    def test_end_refinement_never_moves_start(self):
        config = DetectorConfig()
        candidate = dict(start=10.0, end=15.0, duration=5.0)
        refined = refine_candidate_end(
            candidate, self._dense_burst(start=11.5, active=range(0, 13)),
            [12.0, 12.45, 12.88], config)
        self.assertEqual(refined["start"], candidate["start"])

    def test_rescue_does_not_duplicate_overlapping_coarse_candidate(self):
        existing = [dict(start=9.6, end=11.4)]
        windows = propose_rescue_windows(
            self._coarse(active=21), [10.05, 10.42, 10.78],
            existing, DetectorConfig(), 20)
        self.assertEqual(windows, [])


if __name__ == "__main__":
    unittest.main()
