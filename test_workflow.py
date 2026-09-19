"""Workflow regression checks: opening card, paths, fonts, and local actions."""

import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from intro_card import (SAMPLE_LINES, intro_ass, intro_has_text, intro_duration, select_font,
                        thumbnail_command, thumbnail_path, with_intro_filter)
from ttcut_v2_3 import (Handler, STATE, STATE_LOCK, QUALITY, build_render,
                        default_out, filter_script, intro_filename,
                        is_preview_disconnect, plan, run_job_managed,
                        safe_filename_part, unique_default_out)


DOC = {"players": {"A": "甲", "B": "乙"},
       "scoreboard": {"style": "koko"},
       "events": [{"t": 1, "type": "serve"},
                  {"t": 2, "type": "point", "winner": "A"}]}
SAMPLE_INTRO = {"tournament": "北港媽祖盃全國桌球錦標賽",
                "category": "國小男童一年級以下單打賽",
                "playerA": "許宸愷", "schoolA": "光復國小",
                "playerB": "曾柏誠", "schoolB": "吉林國小"}
EXPECTED_NAME = ("北港媽祖盃全國桌球錦標賽_國小男童一年級以下單打賽_"
                 "許宸愷(光復國小)VS曾柏誠(吉林國小).mp4")


class IntroTests(unittest.TestCase):
    def test_font_fallback_and_duration(self):
        available = {"Kaiti TC", "Songti TC"}
        self.assertEqual(select_font("Missing", available), "Kaiti TC")
        self.assertEqual(select_font("Songti TC", available), "Songti TC")
        self.assertEqual(intro_duration(3, 1.2), 1.2)
        self.assertEqual(intro_duration(3, 10), 3)

    def test_intro_scales_and_escapes_text(self):
        small = intro_ass(SAMPLE_INTRO, 1920, 1080, 3, "Xingkai TC")
        large = intro_ass(SAMPLE_INTRO, 3840, 2160, 3, "Xingkai TC")
        self.assertIn(r"\pos(960,285)\fs136\bord8", small)
        self.assertIn(r"\pos(1920,570)\fs272\bord16", large)
        self.assertIn(r"\pos(550,635)\fs136", small)
        self.assertIn(r"\pos(550,775)\fs92", small)
        self.assertIn(r"\pos(1370,635)\fs136", small)
        self.assertIn(r"\pos(1370,775)\fs92", small)
        self.assertIn(r"\pos(960,635)\fs100", small)
        self.assertEqual(small.count("Dialogue:"), 7)
        thumb = intro_ass(SAMPLE_INTRO, 1280, 720, 3, "Xingkai TC")
        self.assertIn(r"\pos(367,423)\fs91", thumb)
        self.assertIn(r"\pos(913,423)\fs91", thumb)
        self.assertIn(r"\pos(367,517)\fs61", thumb)
        self.assertIn(r"\pos(913,517)\fs61", thumb)
        self.assertIn("0:00:03.00", small)
        self.assertNotIn("{bad}", intro_ass(["{bad}"], 1920, 1080, 3, "Xingkai TC"))

    def test_long_player_shrinks_independently_and_legacy_is_retained(self):
        intro = dict(SAMPLE_INTRO, playerA="非常非常非常長的選手姓名")
        ass = intro_ass(intro, 1920, 1080, 3, "Xingkai TC")
        self.assertIn(r"\pos(1370,635)\fs136", ass)
        self.assertNotIn(r"\pos(550,635)\fs136", ass)
        self.assertIn(r"\pos(2740,1270)\fs272", intro_ass(
            intro, 3840, 2160, 3, "Xingkai TC"))
        self.assertEqual(intro_ass({"lines": SAMPLE_LINES}, 1920, 1080, 3,
                                   "Xingkai TC").count("Dialogue:"), 4)
        self.assertTrue(intro_has_text({"lines": SAMPLE_LINES}))
        self.assertTrue(intro_has_text(SAMPLE_INTRO))

    def test_intro_filter_has_separate_scoreless_segment(self):
        base = filter_script([(0, 4)], "score.ass", "30")
        intro = with_intro_filter(base, "intro.ass", "30", 3)
        self.assertIn("subtitles='score.ass'[matchv]", intro)
        self.assertIn("trim=duration=3.000", intro)
        self.assertIn("subtitles='intro.ass'[introv]", intro)
        self.assertIn("concat=n=2:v=1:a=1[vout][aout]", intro)
        with_fonts = with_intro_filter(base, "intro.ass", "30", 3,
                                       "/intro-fonts", "score.ass", "/score-fonts")
        self.assertIn("subtitles='score.ass':fontsdir='/score-fonts'[matchv]", with_fonts)
        self.assertIn("subtitles='intro.ass':fontsdir='/intro-fonts'[introv]", with_fonts)

    def test_render_on_off_and_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "chosen name.mp4")
            info = {"w": 1920, "h": 1080, "fps": 30, "fps_frac": "30/1",
                    "duration": 5, "codec": "h264", "pix_fmt": "yuv420p",
                    "trc": "", "prim": "", "bitrate": None}
            pl = plan(DOC, {})
            with patch("ttcut_v2_3.probe", return_value=info), \
                 patch("ttcut_v2_3.select_font", return_value="Xingkai TC"), \
                 patch("ttcut_v2_3.font_directory", return_value="/fonts"):
                off, cwd, _ = build_render(DOC, pl, "source.mp4", out,
                                           {"quality": "high"}, "ffmpeg", "ffprobe",
                                           log=lambda _: None)
                self.assertEqual(cwd, tmp)
                self.assertEqual(off[-1], "chosen name.mp4")
                self.assertIn("[ac]", off)
                self.assertNotIn("concat=n=2", off[off.index("-filter_complex") + 1])
                on, _, _ = build_render(DOC, pl, "source.mp4", out,
                                        {"quality": "high", "intro": {
                                            "enabled": True, "duration": 3,
                                            **SAMPLE_INTRO}},
                                        "ffmpeg", "ffprobe", log=lambda _: None)
                self.assertIn("[aout]", on)
                self.assertIn("concat=n=2", on[on.index("-filter_complex") + 1])
                self.assertTrue((Path(tmp) / "chosen name.intro.ass").exists())
            self.assertEqual(thumbnail_path(out), str(Path(tmp) / "chosen name.thumbnail.jpg"))
            thumb = thumbnail_command("ffmpeg", "source.mp4", out,
                                      "chosen name.intro.ass", "/fonts")
            self.assertEqual(thumb[-1], "chosen name.thumbnail.jpg")
            self.assertIn("-frames:v", thumb)

    def test_quality_logic_unchanged_and_default_name(self):
        self.assertFalse(QUALITY["fast"]["force_sw"])
        self.assertFalse(QUALITY["high"]["force_sw"])
        self.assertTrue(QUALITY["max"]["force_sw"])
        self.assertTrue(default_out("/tmp/movie.MOV").endswith("movie.cut.mp4"))


class ActionTests(unittest.TestCase):
    def test_preview_stream_ignores_only_browser_disconnect_errors(self):
        expected = (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ENOBUFS)
        for error_number in expected:
            with self.subTest(errno=error_number):
                self.assertTrue(is_preview_disconnect(OSError(error_number, "closed")))
        self.assertFalse(is_preview_disconnect(OSError(errno.EIO, "disk error")))

        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "source.mp4")
            Path(source).write_bytes(b"preview")
            handler = Handler.__new__(Handler)
            handler.path = "/video"
            handler.headers = {"Range": "bytes=2-5"}
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            with STATE_LOCK:
                previous = dict(STATE)
                STATE.update(video=source, sources=[])
            try:
                handler.wfile = Mock()
                handler.wfile.write.side_effect = OSError(errno.ENOBUFS, "full")
                handler._video()  # Browser abort ends this request quietly.
                handler.send_response.assert_called_with(206)
                handler.send_header.assert_any_call("Content-Range", "bytes 2-5/7")
                handler.send_header.assert_any_call("Content-Length", "4")
                handler.wfile.write.side_effect = OSError(errno.EIO, "disk error")
                with self.assertRaises(OSError) as raised:
                    handler._video()
                self.assertEqual(raised.exception.errno, errno.EIO)
            finally:
                with STATE_LOCK:
                    STATE.update(previous)

    def test_structured_filename_fallback_sanitization_and_duplicates(self):
        self.assertEqual(intro_filename(SAMPLE_INTRO), EXPECTED_NAME)
        self.assertEqual(intro_filename(dict(SAMPLE_INTRO, enabled=False)), EXPECTED_NAME)
        self.assertEqual(intro_filename(dict(SAMPLE_INTRO, schoolB="")), None)
        self.assertEqual(intro_filename(dict(SAMPLE_INTRO, schoolB="()")), None)
        self.assertEqual(intro_filename({"lines": SAMPLE_LINES}), None)
        self.assertEqual(safe_filename_part('  ._賽/\\:*?"<>|\x00\x1f\x7f\u202e事__._  '), '賽_事')
        self.assertEqual(safe_filename_part('臺灣，公開賽(VS)'), '臺灣，公開賽(VS)')
        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / 'raw.MOV')
            self.assertEqual(default_out(video, SAMPLE_INTRO), str(Path(tmp) / EXPECTED_NAME))
            self.assertEqual(default_out(video, dict(SAMPLE_INTRO, playerA='')),
                             str(Path(tmp) / 'raw.cut.mp4'))
            base = Path(default_out(video, SAMPLE_INTRO))
            base.touch()
            self.assertEqual(unique_default_out(video, SAMPLE_INTRO),
                             str(base.with_name(base.stem + '_2.mp4')))
            base.with_name(base.stem + '_2.mp4').touch()
            self.assertEqual(unique_default_out(video, SAMPLE_INTRO),
                             str(base.with_name(base.stem + '_3.mp4')))

    def test_save_as_reset_and_exit_are_local_post_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "source.mp4")
            Path(source).write_bytes(b"video")
            chosen = str(Path(tmp) / "final.mp4")
            handler = Handler.__new__(Handler)
            seen = []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler.send_error = lambda code, *args: seen.append((code, None))
            handler.headers = {"Host": "127.0.0.1:8770",
                               "Origin": "http://127.0.0.1:8770"}
            handler.client_address = ("127.0.0.1", 1000)
            handler.server = type("Server", (), {"shutdown": lambda self: None})()
            handler._body = lambda: {"intro": SAMPLE_INTRO}
            with STATE_LOCK:
                previous = dict(STATE)
                STATE["video"] = source
                STATE["job"] = None
            try:
                handler.path = "/exit"
                handler.do_GET()
                self.assertEqual(seen[-1][0], 404)
                with patch("ttcut_v2_3.native_save_video", return_value=chosen) as save:
                    handler.path = "/save-as"
                    handler.do_POST()
                    self.assertEqual(seen[-1], (200, {"path": chosen, "cancelled": False}))
                    save.assert_called_once_with(str(Path(tmp) / EXPECTED_NAME))
                    self.assertEqual(STATE['custom_out'], chosen)
                with patch('ttcut_v2_3.installed_families', return_value=set()):
                    handler.path = '/state'
                    handler.do_GET()
                self.assertEqual(seen[-1][1]['customOut'], chosen)
                handler.path = '/default-output'
                handler.do_POST()
                self.assertIsNone(STATE['custom_out'])
                handler.path = "/suggest-output"
                handler.do_POST()
                self.assertEqual(seen[-1][1]["path"], str(Path(tmp) / EXPECTED_NAME))
                with patch("ttcut_v2_3.native_save_video", return_value=chosen):
                    handler.path = '/save-as'
                    handler.do_POST()
                handler.path = "/new-match"
                handler.do_POST()
                self.assertEqual(seen[-1][0], 200)
                self.assertIsNone(STATE["video"])
                self.assertIsNone(STATE['custom_out'])
                handler.path = "/exit"
                handler.do_POST()
                self.assertEqual(seen[-1][0], 200)
            finally:
                with STATE_LOCK:
                    STATE.update(previous)

    def test_render_uses_available_default_and_sidecar_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / 'raw.MOV')
            Path(source).write_bytes(b'video')
            base = Path(tmp) / EXPECTED_NAME
            base.touch()
            base.with_name(base.stem + '_2.mp4').touch()
            intro = dict(SAMPLE_INTRO, enabled=True, thumbnail=True, duration=3)
            doc = dict(DOC, intro=intro)
            handler = Handler.__new__(Handler)
            seen = []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler._body = lambda: {'doc': doc, 'opt': {'intro': intro},
                                     'out': str(base), 'customOutput': False}
            with STATE_LOCK:
                previous = dict(STATE)
                STATE.update(video=source, ffmpeg='ffmpeg', ffprobe='ffprobe', job=None)
            try:
                with patch('ttcut_v2_3.build_render', return_value=([], tmp, None)), \
                     patch('ttcut_v2_3.probe', return_value={'duration': 6}), \
                     patch('ttcut_v2_3.threading.Thread') as thread:
                    handler._render()
                    thread.return_value.start.assert_called_once()
                    args = thread.call_args.kwargs['args']
                    with patch('ttcut_v2_3.run_job', side_effect=lambda job, *_: setattr(job, 'state', 'done')):
                        run_job_managed(*args)
                chosen = str(base.with_name(base.stem + '_3.mp4'))
                self.assertEqual(seen[-1][1]['out'], chosen)
                self.assertEqual(seen[-1][1]['thumbnail'], thumbnail_path(chosen))
                tags = Path(chosen.removesuffix('.mp4') + '.tags.json')
                self.assertTrue(tags.exists())
                self.assertEqual(json.loads(tags.read_text())['intro']['tournament'],
                                 SAMPLE_INTRO['tournament'])
                with STATE_LOCK:
                    STATE['job'].state = 'done'
                handler._body = lambda: {'doc': doc, 'opt': {'intro': intro},
                                         'out': str(base), 'customOutput': True}
                with patch('ttcut_v2_3.build_render', return_value=([], tmp, None)), \
                     patch('ttcut_v2_3.probe', return_value={'duration': 6}), \
                     patch('ttcut_v2_3.threading.Thread') as thread:
                    handler._render()
                    args = thread.call_args.kwargs['args']
                    args[4].cleanup()
                self.assertEqual(seen[-1][1]['out'], str(base))
            finally:
                with STATE_LOCK:
                    STATE.update(previous)


if __name__ == "__main__":
    unittest.main()
