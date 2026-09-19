"""Koko v1.3 tournament discovery, review, scoring, and synthetic render."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tournament_highlights import (
    COVER_OFFSET_SECONDS, INTRO_SECONDS,
    TournamentError, apply_review, choose_profile, cover_frame_selection,
    discover_tag_files, intro_background_selection, manifest_for, output_layout,
    output_stem, pair_highlights, prepare_intro_background, prepare_render,
    run_commands, scan_tournament, validate_plan,
)
from ttcut_v2_3 import (DEFAULT_ACCENT, FONT_NAME, FONT_NUM, Job, ass_colour,
                        build_ass, fold_full, probe, probe_audio, read_format,
                        safe_filename_part)


def doc(source, players=("許宸愷（光復國小）", "曾柏誠（吉林國小）"),
        tournament="北港媽祖盃全國桌球錦標賽", events=None, sources=None):
    result = {
        "version": 2, "generator": "ttcut V2.3", "source": source,
        "players": {"A": players[0], "B": players[1]}, "firstServer": "A",
        "format": {"pointsPerGame": 11, "deuce": "standard", "cap": 12},
        "start": {"games": {"A": 0, "B": 0}, "points": {"A": 0, "B": 0},
                  "handicapScope": "every"},
        "intro": {"tournament": tournament},
        "events": events if events is not None else [
            {"t": 1.0, "type": "serve"},
            {"t": 2.0, "type": "point", "winner": "A", "highlight": True},
        ],
    }
    if sources is not None:
        result["sources"] = [{"path": value} for value in sources]
    return result


def write_doc(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TournamentDiscoveryTests(unittest.TestCase):
    def test_recursive_modern_legacy_unrelated_zero_and_bad_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "北港媽祖盃"
            a = root / "A"; b = root / "B"; c = root / "C"
            for match in (a, b, c):
                match.mkdir(parents=True)
                (match / f"{match.name}.mp4").touch()
            write_doc(a / "ttcut-data" / "A.tags.json", doc("A.mp4"))
            write_doc(b / "B.tags.json", doc("B.mp4", players=("許宸愷（光復國小）", "王小明（東園國小）")))
            write_doc(c / "ttcut-data" / "C.tags.json", doc("C.mp4", events=[]))
            write_doc(root / "other.json", {"hello": "world"})
            write_doc(root / "looks.tags.json", {"hello": "world"})
            (root / "broken.tags.json").write_text("{broken", encoding="utf-8")

            found = discover_tag_files(root)
            self.assertEqual(len(found), 5)
            scan = scan_tournament(root, fold_full, read_format)
            self.assertEqual(scan["scanned_matches"], 3)
            self.assertEqual(scan["total_highlights"], 2)
            self.assertEqual(len(scan["matches"]), 2)  # zero-highlight match omitted
            self.assertEqual(scan["metadata"]["protagonist"], "許宸愷")
            self.assertEqual(scan["metadata"]["school"], "光復國小")
            self.assertEqual(sum(w["type"] == "malformed-tags" for w in scan["warnings"]), 1)

    def test_pairing_uses_last_serve_in_open_rally_and_reports_missing(self):
        events = [
            {"t": 1, "type": "serve"}, {"t": 1.5, "type": "serve"},
            {"t": 2, "type": "point", "winner": "A", "highlight": True},
            {"t": 3, "type": "point", "winner": "B", "highlight": True},
        ]
        pairs, invalid = pair_highlights(events)
        self.assertEqual([p["serve_time"] for p in pairs], [1.5])
        self.assertEqual([p["point_time"] for p in invalid], [3])

    def test_review_selection_and_order_do_not_mutate_scan(self):
        scan = {"matches": [
            {"id": "a", "highlights": [{"point_id": "0:1.000", "selected": True},
                                           {"point_id": "1:2.000", "selected": True}]},
            {"id": "b", "highlights": [{"point_id": "0:3.000", "selected": True}]},
        ]}
        original = copy.deepcopy(scan)
        plan = apply_review(scan, {"match_order": ["b", "a"],
            "selected": {"a": {"0:1.000": False, "1:2.000": True},
                         "b": {"0:3.000": True}},
            "highlight_order": {"a": ["1:2.000", "0:1.000"]}})
        self.assertEqual([m["id"] for m in plan["matches"]], ["b", "a"])
        self.assertEqual([h["point_id"] for h in plan["matches"][1]["highlights"]], ["1:2.000"])
        self.assertEqual([h["point_id"] for m in plan["matches"] for h in m["highlights"]],
                         ["0:3.000", "1:2.000"])
        self.assertEqual(scan, original)

    def test_cover_uses_first_selected_item_after_review_reorder_and_uncheck(self):
        scan = {"matches": [
            {"id": "a", "highlights": [{"point_id": "a1", "selected": True},
                                          {"point_id": "a2", "selected": True}]},
            {"id": "b", "highlights": [{"point_id": "b1", "selected": True}]},
        ]}
        plan = apply_review(scan, {"match_order": ["b", "a"],
            "selected": {"b": {"b1": False}, "a": {"a1": False, "a2": True}},
            "highlight_order": {"a": ["a2", "a1"]}})
        plan["matches"][0]["highlights"][0].update(start=4.0, end=7.0)
        plan["matches"][0]["source_info"] = [
            {"path": "/original-a.mp4", "offset": 0.0, "end": 10.0}]
        background = intro_background_selection(plan)
        cover = cover_frame_selection(plan)
        self.assertEqual(background["item"]["point_id"], "a2")
        self.assertEqual(background["start"], 4.0)
        self.assertEqual(cover["item"]["point_id"], "a2")
        self.assertEqual(cover["source"]["path"], "/original-a.mp4")
        self.assertAlmostEqual(cover["global_time"], 4.0 + COVER_OFFSET_SECONDS)
        self.assertAlmostEqual(cover["local_time"], 4.0 + COVER_OFFSET_SECONDS)

    def test_cover_global_time_resolves_single_and_multifile_boundary(self):
        single = {"matches": [{"highlights": [{"point_id": "p", "start": .5, "end": 3.0}],
                               "source_info": [{"path": "one.mp4", "offset": 0.0,
                                                "end": 4.0}]}]}
        cover = cover_frame_selection(single)
        self.assertEqual(cover["source_index"], 0)
        self.assertAlmostEqual(cover["global_time"], 1.75)
        self.assertAlmostEqual(cover["local_time"], 1.75)

        boundary = {"matches": [{"highlights": [{"point_id": "p", "start": .25,
                                                   "end": 2.8}],
                                  "source_info": [
                                      {"path": "one.mp4", "offset": 0.0, "end": 1.5},
                                      {"path": "two.mp4", "offset": 1.5, "end": 3.0}]}]}
        cover = cover_frame_selection(boundary)
        self.assertEqual(cover["source_index"], 1)
        self.assertEqual(cover["source"]["path"], "two.mp4")
        self.assertAlmostEqual(cover["global_time"], 1.5)
        self.assertAlmostEqual(cover["local_time"], 0.0)

    def test_intro_background_window_maps_single_and_cross_file_sources(self):
        single = {"matches": [{"highlights": [{"point_id": "p", "start": .5,
                                                  "end": 4.0}],
                               "source_info": [{"path": "one.mp4", "offset": 0.0,
                                                "end": 5.0}]}]}
        background = intro_background_selection(single)
        self.assertEqual((background["start"], background["end"]), (.5, 3.5))
        self.assertEqual([(p[0]["path"], p[1], p[2]) for p in background["overlaps"]],
                         [("one.mp4", .5, 3.0)])

        crossing = {"matches": [{"highlights": [{"point_id": "p", "start": .25,
                                                    "end": 3.25}],
                                  "source_info": [
                                      {"path": "one.mp4", "offset": 0.0, "end": 1.5},
                                      {"path": "two.mp4", "offset": 1.5, "end": 4.0}]}]}
        background = intro_background_selection(crossing)
        self.assertEqual([(p[0]["path"], p[1], p[2]) for p in background["overlaps"]],
                         [("one.mp4", .25, 1.25), ("two.mp4", 0.0, 1.75)])

    def test_scoreboard_states_cover_mid_game_later_game_and_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); match = root / "match"; match.mkdir(); (match / "m.mp4").touch()
            events = [
                {"t": 1, "type": "serve"},
                {"t": 2, "type": "point", "winner": "A", "highlight": True},
                {"t": 3, "type": "serve"},
                {"t": 4, "type": "point", "winner": "A", "highlight": True},
                {"t": 5, "type": "serve"},
                {"t": 6, "type": "point", "winner": "B", "highlight": True},
            ]
            payload = doc("m.mp4", events=events)
            payload["format"] = {"pointsPerGame": 2, "deuce": "capped", "cap": 2}
            write_doc(match / "ttcut-data" / "m.tags.json", payload)
            highlights = scan_tournament(root, fold_full, read_format)["matches"][0]["highlights"]
            self.assertEqual(highlights[0]["score_before"], [0, 0, 0, 0])
            self.assertEqual(highlights[0]["score_after"], [0, 0, 1, 0])
            self.assertEqual(highlights[1]["score_after"], [1, 0, 2, 0])  # game winner
            self.assertEqual(highlights[2]["score_before"], [1, 0, 2, 0])
            self.assertEqual(highlights[2]["score_after"], [1, 0, 0, 1])  # later game

    def test_validation_crosses_source_boundary_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [str(Path(folder) / name) for name in ("1.mp4", "2.mp4")]
            for path in paths: Path(path).touch()
            match = {"id":"m", "label":"甲 vs 乙", "sources":paths,
                     "highlights":[{"point_id":"1:2.200", "serve_time":.7,
                                    "point_time":2.2}]}
            plan = {"selected_highlights":1, "matches":[match]}
            info = lambda path, _: {"duration":1.5,"w":1920,"h":1080,"fps":60,
                "fps_frac":"60/1","codec":"h264","pix_fmt":"yuv420p","trc":""}
            checked = validate_plan(plan, info, lambda *_:{"codec_name":"aac"})
            item = checked["matches"][0]["highlights"][0]
            self.assertEqual(item["start"], 0)
            self.assertEqual(item["end"], 3.0)
            self.assertEqual(len(checked["matches"][0]["source_info"]), 2)
            with self.assertRaisesRegex(TournamentError, "沒有勾選"):
                validate_plan({"selected_highlights":0,"matches":[]}, info, lambda *_:None)
            os.unlink(paths[0])
            with self.assertRaisesRegex(TournamentError, "找不到來源影片"):
                validate_plan(plan, info, lambda *_:None)

    def test_metadata_filename_layout_manifest_and_profile(self):
        metadata = {"tournament":"北港/媽祖盃", "protagonist":"許:宸愷"}
        self.assertEqual(output_stem(metadata, safe_filename_part), "北港_媽祖盃_許_宸愷精彩好球")
        layout = output_layout("/tmp/賽事_甲精彩好球.mp4")
        self.assertTrue(layout["out"].endswith("賽事_甲精彩好球/賽事_甲精彩好球.mp4"))
        self.assertTrue(layout["thumbnail"].endswith(".thumbnail.jpg"))
        self.assertTrue(layout["manifest"].endswith("ttcut-data/賽事_甲精彩好球.highlights.json"))
        plan = {"matches":[{"tags":"/m.tags.json","highlights":[
            {"point_time":2,"point_id":"1:2.000"}], "source_info":[
            {"w":3840,"h":2160,"fps":60,"fps_frac":"60/1","audio":None}]}]}
        self.assertEqual(choose_profile(plan)["size"], (3840,2160))
        mixed = copy.deepcopy(plan)
        mixed["matches"][0]["source_info"].append(
            {"w":1280,"h":720,"fps":29.97,"fps_frac":"30000/1001",
             "audio":{"codec_name":"pcm_s16le","sample_rate":"44100","channels":1}})
        self.assertEqual(choose_profile(mixed),
                         {"size":(1920,1080),"fps":30.0,"normalized":True})
        manifest = manifest_for(plan, metadata, {"quality":"high"})
        self.assertEqual(manifest["type"], "koko-highlight-project")
        self.assertEqual(manifest["presentation"]["intro_seconds"], INTRO_SECONDS)
        self.assertFalse(manifest["presentation"]["match_cards"])
        self.assertFalse(manifest["presentation"]["scoreboard_on_intro"])
        self.assertNotIn("events", json.dumps(manifest))

    def test_cover_extraction_failure_uses_dark_intro_and_thumbnail(self):
        plan = {"matches": [{
            "metadata": {"playerA": "甲", "playerB": "乙"},
            "scoreboard_names": ["甲", "乙"],
            "source_info": [{"path": "/missing.mp4", "offset": 0.0, "end": 3.0}],
            "highlights": [{"point_id": "p", "start": 0.0, "end": 2.0,
                            "duration": 2.0, "point_time": 1.0,
                            "score_before": [0, 0, 0, 0],
                            "score_after": [0, 0, 1, 0]}]}]}
        metadata = {"tournament": "盃賽", "title": "精彩好球",
                    "protagonist": "甲", "school": "學校"}
        layout = {"out": "/tmp/out.mp4", "thumbnail": "/tmp/thumb.jpg"}
        with mock.patch("tournament_highlights.subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, ["ffmpeg"])):
            temp, commands, clips = prepare_render(
                plan, metadata, {"quality": "fast", "size": (320, 180), "fps": 30},
                layout, "ffmpeg", build_ass, FONT_NAME, FONT_NUM,
                ass_colour(DEFAULT_ACCENT), "libx264")
        try:
            self.assertEqual(len(clips), 2)  # intro + rally; never a match card
            self.assertFalse(any("-match.mp4" in str(value)
                                 for command in commands for value in command))
            self.assertIn("color=c=#04121F", " ".join(commands[0]))
            self.assertIn("color=c=#04121F", " ".join(commands[-1]))
            self.assertIn(f"{INTRO_SECONDS:.3f}", commands[0])
            self.assertNotIn("score.ass", " ".join(commands[0]))
        finally:
            temp.cleanup()

    def test_natural_moving_background_is_shared_by_intro_and_thumbnail_then_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"; source.touch()
            plan = {"matches": [{
                "metadata": {"playerA": "甲", "playerB": "乙"},
                "scoreboard_names": ["甲", "乙"],
                "source_info": [{"path": str(source), "offset": 0.0, "end": 3.0}],
                "highlights": [{"point_id": "p", "start": 0.0, "end": 2.0,
                                "duration": 2.0, "point_time": 1.0,
                                "score_before": [0, 0, 0, 0],
                                "score_after": [0, 0, 1, 0]}]}]}
            layout = {"out": "/tmp/out.mp4", "thumbnail": "/tmp/thumb.jpg"}

            def fake_prepare(_ffmpeg, _selection, path, _size, _fps):
                Path(path).write_bytes(b"moving-video")
                return str(path)

            with mock.patch("tournament_highlights.prepare_intro_background",
                            side_effect=fake_prepare):
                temp, commands, clips = prepare_render(
                    plan, {"title": "精彩好球"},
                    {"quality": "fast", "size": (320, 180), "fps": 30}, layout,
                    "ffmpeg", build_ass, FONT_NAME, FONT_NUM,
                    ass_colour(DEFAULT_ACCENT), "libx264")
            background_path = str(Path(temp.name) / "intro-background.mkv")
            self.assertIn(background_path, commands[0])
            self.assertIn(background_path, commands[-1])
            self.assertNotIn("-loop", commands[0])
            self.assertEqual(commands[-1][commands[-1].index("-ss") + 1],
                             f"{COVER_OFFSET_SECONDS:.3f}")
            rendered = " ".join(value for command in (commands[0], commands[-1])
                                for value in command)
            for old_filter in ("boxblur", "brightness", "saturation", "drawbox"):
                self.assertNotIn(old_filter, rendered)
            self.assertEqual(len(clips), 2)
            self.assertTrue(Path(background_path).is_file())
            temp.cleanup()
            self.assertFalse(Path(background_path).exists())

    def test_intro_background_setup_failure_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "background.mkv"
            target.write_bytes(b"partial")
            source = {"path": "/bad.mp4", "offset": 0.0, "end": 1.0, "audio": None}
            selection = {"overlaps": [(source, 0.0, 1.0)], "content_duration": 1.0,
                         "duration": INTRO_SECONDS}
            with mock.patch("tournament_highlights.subprocess.run",
                            side_effect=subprocess.CalledProcessError(1, ["ffmpeg"])):
                self.assertIsNone(prepare_intro_background(
                    "ffmpeg", selection, target, (320, 180), 30))
            self.assertFalse(target.exists())

    def test_intro_background_command_joins_sources_and_freezes_short_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "background.mkv"
            sources = [
                {"path": "/one.mp4", "offset": 0.0, "end": 1.0, "audio": None},
                {"path": "/two.mp4", "offset": 1.0, "end": 2.0, "audio": None},
            ]
            selection = {"overlaps": [(sources[0], .25, .75),
                                       (sources[1], 0.0, 1.0)],
                         "content_duration": 1.75, "duration": INTRO_SECONDS}
            captured = []

            def fake_run(command, **_kwargs):
                captured.extend(command)
                target.write_bytes(b"lossless-moving-background")

            with mock.patch("tournament_highlights.subprocess.run", side_effect=fake_run):
                self.assertEqual(prepare_intro_background(
                    "ffmpeg", selection, target, (320, 180), 30), str(target))
            command = " ".join(captured)
            self.assertIn("/one.mp4", captured)
            self.assertIn("/two.mp4", captured)
            self.assertIn("concat=n=2:v=1:a=1", command)
            self.assertIn("tpad=stop_mode=clone:stop_duration=1.250000", command)
            for old_filter in ("boxblur", "brightness", "saturation", "drawbox"):
                self.assertNotIn(old_filter, command)


@unittest.skipUnless(Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg").is_file(),
                     "ffmpeg-full is not installed")
class SyntheticTournamentE2E(unittest.TestCase):
    FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    FFPROBE = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"

    def make_video(self, path, colour, frequency, duration=1.5):
        subprocess.run([self.FFMPEG,"-y","-f","lavfi","-i",
                        f"color=c={colour}:s=320x180:r=30:d={duration}",
                        "-f","lavfi","-i",f"sine=frequency={frequency}:duration={duration}",
                        "-vf", "noise=alls=20:allf=t+u",
                        "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(path)],
                       check=True, capture_output=True)

    def corner_rgb(self, path, at=None):
        command = [self.FFMPEG, "-v", "error"]
        if at is not None:
            command += ["-ss", str(at)]
        command += ["-i", str(path), "-vf", "crop=2:2:0:0,format=rgb24",
                    "-frames:v", "1", "-f", "rawvideo", "pipe:1"]
        pixel = subprocess.run(command, check=True, capture_output=True).stdout
        self.assertGreaterEqual(len(pixel), 3)
        return tuple(pixel[:3])

    def frame_rgb(self, path, at):
        return subprocess.run(
            [self.FFMPEG, "-v", "error", "-ss", str(at), "-i", str(path),
             "-vf", "scale=160:90,format=rgb24", "-frames:v", "1",
             "-f", "rawvideo", "pipe:1"], check=True, capture_output=True).stdout

    def test_two_match_three_highlight_render_with_multifile_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "盃賽"; a = root / "A"; b = root / "B"
            a.mkdir(parents=True); b.mkdir()
            a1, a2, bv = a/"A-1.mp4", a/"A-2.mp4", b/"B.mp4"
            self.make_video(a1,"red",440); self.make_video(a2,"blue",550); self.make_video(bv,"green",660,1.8)
            events_a = [{"t":.9,"type":"serve"},{"t":1.6,"type":"point","winner":"A","highlight":True},
                        {"t":2.0,"type":"serve"},{"t":2.5,"type":"point","winner":"B","highlight":True}]
            events_b = [{"t":.2,"type":"serve"},{"t":1.0,"type":"point","winner":"A","highlight":True}]
            write_doc(a/"ttcut-data"/"A.tags.json", doc("A-1.mp4",events=events_a,sources=[str(a1),str(a2)]))
            write_doc(b/"ttcut-data"/"B.tags.json", doc("B.mp4",players=("許宸愷（光復國小）","王小明（東園國小）"),events=events_b))
            scan = scan_tournament(root, fold_full, read_format)
            ids = {match["id"]: match for match in scan["matches"]}
            a_id = next(key for key in ids if key.startswith("A/"))
            b_id = next(key for key in ids if key.startswith("B/"))
            first_a = ids[a_id]["highlights"][0]["point_id"]
            plan = apply_review(scan, {"match_order": [b_id, a_id],
                "selected": {a_id: {first_a: False}}})
            plan = validate_plan(plan, probe, probe_audio, self.FFPROBE)
            cover = cover_frame_selection(plan)
            self.assertEqual(cover["match"]["id"], b_id)
            self.assertEqual(cover["source"]["path"], str(bv))
            profile = choose_profile(plan)
            metadata = scan["metadata"]
            layout = output_layout(str(root / (output_stem(metadata,safe_filename_part)+".mp4")))
            os.makedirs(layout["folder"])
            temp, commands, clips = prepare_render(plan,metadata,
                {"quality":"fast","size":profile["size"],"fps":profile["fps"]},
                layout,self.FFMPEG,build_ass,FONT_NAME,FONT_NUM,ass_colour(DEFAULT_ACCENT),"libx264")
            job = Job(layout["out"],len(commands))
            temp_path = temp.name
            run_commands(job,commands,temp.name,temp,layout["manifest"],
                         manifest_for(plan,metadata,{"quality":"fast"}))
            self.assertEqual(job.state,"done",job.log)
            self.assertFalse(os.path.exists(temp_path))
            self.assertTrue(Path(layout["out"]).is_file())
            self.assertTrue(Path(layout["thumbnail"]).is_file())
            thumb = probe(layout["thumbnail"],self.FFPROBE)
            self.assertEqual((thumb["w"],thumb["h"]),(1280,720))
            intro_rgb = self.corner_rgb(layout["out"], .5)
            thumb_rgb = self.corner_rgb(layout["thumbnail"])
            source_intro_rgb = self.corner_rgb(bv, .5)
            source_thumb_rgb = self.corner_rgb(bv, COVER_OFFSET_SECONDS)
            self.assertGreater(intro_rgb[1], max(intro_rgb[0], intro_rgb[2]))
            self.assertGreater(thumb_rgb[1], max(thumb_rgb[0], thumb_rgb[2]))
            self.assertLess(max(abs(a-b) for a,b in zip(intro_rgb, source_intro_rgb)), 18)
            self.assertLess(max(abs(a-b) for a,b in zip(thumb_rgb, source_thumb_rgb)), 18)
            self.assertNotEqual(self.frame_rgb(layout["out"], .25),
                                self.frame_rgb(layout["out"], 1.25))
            self.assertEqual(len(clips),3)  # intro + two selected rallies; no match cards
            self.assertFalse(any("-match.mp4" in str(path) for path in clips))
            saved = json.loads(Path(layout["manifest"]).read_text())
            self.assertEqual([len(m["selected_points"]) for m in saved["matches"]],[1,1])
            self.assertFalse(saved["presentation"]["match_cards"])
            self.assertEqual(saved["presentation"]["cover"]["selected_point_id"],
                             plan["matches"][0]["highlights"][0]["point_id"])
            media = probe(layout["out"],self.FFPROBE)
            self.assertGreater(media["duration"],5)
            self.assertLess(media["duration"],8)
            self.assertIsNotNone(probe_audio(layout["out"],self.FFPROBE))
            leftovers = [p.name for p in root.iterdir() if p.name in ("temp.mp4","concat.txt","filter.txt")]
            self.assertEqual(leftovers,[])

            fallback_layout = output_layout(str(root / "fallback.mp4"))
            os.makedirs(fallback_layout["folder"])
            with mock.patch("tournament_highlights.prepare_intro_background", return_value=None):
                fallback_temp, fallback_commands, _ = prepare_render(
                    plan, metadata,
                    {"quality":"fast", "size":profile["size"], "fps":profile["fps"]},
                    fallback_layout, self.FFMPEG, build_ass, FONT_NAME, FONT_NUM,
                    ass_colour(DEFAULT_ACCENT), "libx264")
            fallback_job = Job(fallback_layout["out"], len(fallback_commands))
            fallback_temp_path = fallback_temp.name
            run_commands(fallback_job, fallback_commands, fallback_temp.name, fallback_temp,
                         fallback_layout["manifest"],
                         manifest_for(plan, metadata, {"quality":"fast"}))
            self.assertEqual(fallback_job.state, "done", fallback_job.log)
            self.assertTrue(Path(fallback_layout["thumbnail"]).is_file())
            self.assertFalse(os.path.exists(fallback_temp_path))


if __name__ == "__main__":
    unittest.main()
