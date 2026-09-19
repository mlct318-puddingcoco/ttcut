"""Koko v1.3 tournament discovery, review, scoring, and synthetic render."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tournament_highlights import (
    TournamentError, apply_review, choose_profile, discover_tag_files,
    manifest_for, output_layout, output_stem, pair_highlights, prepare_render,
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
        self.assertEqual(scan, original)

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
        self.assertNotIn("events", json.dumps(manifest))


@unittest.skipUnless(Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg").is_file(),
                     "ffmpeg-full is not installed")
class SyntheticTournamentE2E(unittest.TestCase):
    FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    FFPROBE = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"

    def make_video(self, path, colour, frequency, duration=1.5):
        subprocess.run([self.FFMPEG,"-y","-f","lavfi","-i",
                        f"color=c={colour}:s=320x180:r=30:d={duration}",
                        "-f","lavfi","-i",f"sine=frequency={frequency}:duration={duration}",
                        "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(path)],
                       check=True, capture_output=True)

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
            plan = apply_review(scan,{})
            plan = validate_plan(plan, probe, probe_audio, self.FFPROBE)
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
            self.assertEqual(len(clips),6)  # intro + two cards + three rallies
            saved = json.loads(Path(layout["manifest"]).read_text())
            self.assertEqual([len(m["selected_points"]) for m in saved["matches"]],[2,1])
            media = probe(layout["out"],self.FFPROBE)
            self.assertGreater(media["duration"],7)
            self.assertIsNotNone(probe_audio(layout["out"],self.FFPROBE))
            leftovers = [p.name for p in root.iterdir() if p.name in ("temp.mp4","concat.txt","filter.txt")]
            self.assertEqual(leftovers,[])


if __name__ == "__main__":
    unittest.main()
