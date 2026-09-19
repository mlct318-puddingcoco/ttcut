"""ASS layout and style-selection regression checks for the two scoreboards."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ttcut_v2_3 import (
    KOKO_GAMES_BG, KOKO_GAMES_W, KOKO_NAME_FS, KOKO_NAME_MAX_W,
    KOKO_NAME_MIN_FS, KOKO_NAME_MIN_W, KOKO_NUM_FS, KOKO_POINTS_BG,
    KOKO_POINTS_W, KOKO_WHITE, build_ass, build_render, fold_full,
    koko_layout, koko_name_size, koko_name_width, plan, scoreboard_style_for,
)


NAMES = ("許宸愷（光復國小）", "曾柏誠（○○國小）")


def sample(states=None, names=NAMES, size=(1920, 1080), style="koko"):
    return build_ass(states or [(None, 1, 0, 10, 8)], lambda t: t, 3.0,
                     names, *size, scoreboard_style=style)


def dialogue(ass, layer=None):
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    return [line for line in lines if line.startswith(f"Dialogue: {layer},")] if layer is not None else lines


class ScoreboardTests(unittest.TestCase):
    def test_koko_has_two_rows_and_three_fixed_cells_per_row(self):
        ass = sample()
        geo = koko_layout(1920, 1080, NAMES)
        static = dialogue(ass, 0)
        self.assertEqual(len(static), 6)
        self.assertEqual(sum(KOKO_GAMES_BG in line for line in static), 2)
        self.assertEqual(sum(KOKO_POINTS_BG in line for line in static), 2)
        self.assertEqual(len([line for line in dialogue(ass, 1) if NAMES[0] in line or NAMES[1] in line]), 2)
        self.assertIn(f"\\pos({geo['games_cx']},", ass)
        self.assertIn(f"\\pos({geo['points_cx']},", ass)
        self.assertIn(f"\\1c{KOKO_WHITE}", ass)

    def test_ass_updates_both_scores_and_game_count_from_existing_fold(self):
        events = ([dict(t=0.5, type="point", winner="B")]
                  + [dict(t=0.6 + i * 0.1, type="point", winner="A")
                     for i in range(11)])
        scoring = fold_full(events, dict(target=11, deuce="standard", cap=12),
                            dict(games=[0, 0], points=[0, 0], scope="every"))
        ass = sample(scoring["states"])
        final = [line for line in dialogue(ass, 2)
                 if line.startswith("Dialogue: 2,0:00:01.60,")]
        self.assertEqual(len(final), 4)
        geo = koko_layout(1920, 1080, NAMES)
        self.assertTrue(any(f"\\pos({geo['games_cx']},926)" in line and line.endswith("}1")
                            for line in final))   # A wins one game
        self.assertTrue(any(f"\\pos({geo['points_cx']},926)" in line and line.endswith("}11")
                            for line in final))   # A reaches 11 points
        self.assertTrue(any(f"\\pos({geo['points_cx']},986)" in line and line.endswith("}1")
                            for line in final))   # B's earlier point remains
        self.assertEqual(scoring["cur"]["gA"], 1)
        self.assertEqual(scoring["cur"]["b"], 1)
        self.assertIn("0:00:00.50", ass)
        self.assertIn("0:00:01.60", ass)

    def test_numeric_positions_stay_fixed_from_zero_to_eleven(self):
        states = [(None, 0, 0, 0, 0), (1, 0, 0, 9, 0),
                  (2, 0, 0, 10, 0), (3, 1, 0, 11, 0)]
        ass = build_ass(states, lambda t: t, 4, NAMES, 1920, 1080,
                        scoreboard_style="koko")
        positions = {tuple(map(int, pair)) for line in dialogue(ass, 2)
                     for pair in re.findall(r"\\pos\((\d+),(\d+)\)\\fs42", line)}
        geo = koko_layout(1920, 1080, NAMES)
        self.assertEqual(positions, {(geo["games_cx"], 926), (geo["points_cx"], 926),
                                     (geo["games_cx"], 986), (geo["points_cx"], 986)})

    def test_name_width_adapts_to_longer_label_and_is_shared_by_both_rows(self):
        short = ("甲", "乙")
        typical = ("許宸愷（光復國小）", "陳季泓（東園國小）")
        mixed = (short[0], typical[1])
        self.assertEqual(koko_name_width(short), KOKO_NAME_MIN_W)
        self.assertEqual(koko_name_width(typical), 354)
        self.assertEqual(koko_name_width(mixed), koko_name_width(typical))
        self.assertLess(koko_name_width(typical), KOKO_NAME_MAX_W)
        ass = sample(names=mixed)
        geo = koko_layout(1920, 1080, mixed)
        name_cells = [line for line in dialogue(ass, 0)
                      if "m 0 0 l 354 0 l 354 60" in line]
        self.assertEqual(len(name_cells), 2)
        self.assertIn(f"\\clip(88,896,{64 + geo['name_w'] - 24},956)", ass)
        self.assertIn(f"\\clip(88,956,{64 + geo['name_w'] - 24},1016)", ass)

    def test_long_name_caps_width_and_only_name_font_shrinks(self):
        long_name = "許宸愷（光復國小桌球代表隊暨校友聯隊）"
        names = (long_name, NAMES[1])
        self.assertEqual(koko_name_width(names), KOKO_NAME_MAX_W)
        self.assertLess(koko_name_size(long_name, KOKO_NAME_MAX_W), KOKO_NAME_FS)
        self.assertGreaterEqual(koko_name_size(long_name, KOKO_NAME_MAX_W),
                                KOKO_NAME_MIN_FS)
        geo = koko_layout(1920, 1080, names)
        self.assertEqual(geo["games_w"], KOKO_GAMES_W)
        self.assertEqual(geo["points_w"], KOKO_POINTS_W)
        ass = sample(names=names)
        self.assertTrue(all(f"\\fs{KOKO_NUM_FS}" in line
                            for line in dialogue(ass, 2)))
        self.assertIn(r"\clip(", ass)

    def test_original_scoreboard_is_still_selectable(self):
        old = sample(style="ttcut")
        self.assertIn("&HEDE3D6&", old)
        self.assertNotIn(KOKO_GAMES_BG, old)
        self.assertNotIn(KOKO_POINTS_BG, old)
        self.assertEqual(scoreboard_style_for({}, {}), "ttcut")
        self.assertEqual(scoreboard_style_for({"scoreboard": {"style": "koko"}}, {}), "koko")
        self.assertEqual(scoreboard_style_for({"scoreboard": {"style": "koko"}},
                                              {"scoreboard_style": "ttcut"}), "ttcut")

    def test_render_path_reads_saved_style_and_allows_override(self):
        doc = dict(players={"A": NAMES[0], "B": NAMES[1]},
                   scoreboard={"style": "koko"}, fps=30,
                   events=[{"t": 1.0, "type": "serve"},
                           {"t": 2.0, "type": "point", "winner": "A"}])
        settings = {"encoder": "libx264", "hwaccel": "none"}
        pl = plan(doc, settings)
        self.assertTrue(pl["ok"])
        with tempfile.TemporaryDirectory() as folder, patch("ttcut_v2_3.probe", return_value=None):
            for style in ("koko", "ttcut"):
                opt = {**settings, **({"scoreboard_style": "ttcut"} if style == "ttcut" else {})}
                out = str(Path(folder) / f"{style}.mp4")
                build_render(doc, pl, "dummy.mp4", out, opt, "ffmpeg", "ffprobe",
                             log=lambda _: None)
                ass = (Path(folder) / f"{style}.ass").read_text()
                self.assertEqual(KOKO_GAMES_BG in ass, style == "koko")

    def test_1080p_and_4k_scale_all_cells_and_margins(self):
        small = koko_layout(1920, 1080, NAMES)
        large = koko_layout(3840, 2160, NAMES)
        for key in ("x", "name_w", "games_w", "points_w", "row_h",
                    "games_cx", "points_cx"):
            self.assertEqual(large[key], small[key] * 2)
        self.assertEqual(2160 - large["y"] - 2 * large["row_h"], 128)
        self.assertIn("PlayResX: 3840", sample(size=(3840, 2160)))


if __name__ == "__main__":
    unittest.main()
