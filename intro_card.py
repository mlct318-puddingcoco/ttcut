"""Optional opening title and matching YouTube thumbnail for ttcut."""

import os
import re
import subprocess

FONT_PREFERENCES = ("Xingkai TC", "Kaiti TC", "Songti TC", "PingFang TC")
SAMPLE_LINES = ("城市盃全國桌球錦標賽", "國小男生二年級團體賽",
                "許宸愷 VS 曾柏誠", "光復國小    吉林國小")
INTRO_FIELDS = ("tournament", "category", "playerA", "schoolA", "playerB", "schoolB")
NORMAL_INTRO_BASE_SIZE = 128
NORMAL_INTRO_MIN_SIZE = 64
NORMAL_INTRO_WIDTHS = {
    "tournament": 1660,
    "category": 1660,
    "playerA": 640,
    "playerB": 640,
    "schoolA": 640,
    "schoolB": 640,
}


def installed_families():
    """Ask fontconfig what this machine actually has; no bundled font required."""
    try:
        result = subprocess.run(["fc-list", "-f", "%{family}\n"],
                                capture_output=True, text=True, check=True)
        return {name.strip().replace(r"\-", "-")
                for line in result.stdout.splitlines()
                for name in line.split(",") if name.strip()}
    except (OSError, subprocess.CalledProcessError):
        return set()


def select_font(custom="", available=None):
    available = installed_families() if available is None else set(available)
    if custom and custom in available:
        return custom
    return next((name for name in FONT_PREFERENCES if name in available),
                "PingFang TC" if not available else sorted(available)[0])


def font_directory(font):
    """Give libass the actual installed face; macOS CoreText can omit optional faces."""
    try:
        result = subprocess.run(["fc-match", "-f", "%{file}", font],
                                capture_output=True, text=True, check=True)
        path = result.stdout.strip()
        return os.path.dirname(path) if os.path.isfile(path) else None
    except (OSError, subprocess.CalledProcessError):
        return None


def intro_duration(requested, source_duration):
    seconds = max(0.1, min(30.0, float(requested)))
    return min(seconds, source_duration) if source_duration else seconds


def ass_time(seconds):
    cs = round(seconds * 100)
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def intro_has_text(intro):
    return any(str(intro.get(k, "")).strip() for k in INTRO_FIELDS) or any(
        str(v).strip() for v in intro.get("lines", []))


def _text_units(value):
    """Approximate glyph advances for a bounded ASS text box (CJK is one em)."""
    return sum(1 if ord(ch) > 0x2e80 else .58 for ch in value)


def _size_for(value, base, max_width, minimum=28):
    """Fit one unwrapped row deterministically, without enlarging short text."""
    return min(base, max(minimum, int(max_width / max(_text_units(value), 1))))


def structured_intro_sizes(intro):
    """Return the normal-match row sizes at the 1920x1080 design baseline."""
    return {
        field: _size_for(str(intro.get(field, "")), NORMAL_INTRO_BASE_SIZE,
                         max_width, NORMAL_INTRO_MIN_SIZE)
        for field, max_width in NORMAL_INTRO_WIDTHS.items()
    }


def intro_ass(intro, width, height, duration, font):
    """Resolution-aware title and independent player columns; legacy lines stay visible."""
    scale = min(width / 1920, height / 1080)
    structured = isinstance(intro, dict) and any(k in intro for k in INTRO_FIELDS)
    lines = intro.get("lines", []) if isinstance(intro, dict) else intro
    clean_font = font.replace(",", " ").replace("\n", " ")
    bold = 0 if font in ("BiauKaiTC", "BiauKai", "Kaiti TC") else 1
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Intro,{clean_font},100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,8,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    def add(line, x, y, size, max_width, minimum=28):
        safe = re.sub(r"[{}\\\r\n]", " ", str(line)).strip()
        if not safe:
            return
        size = max(16, round(_size_for(safe, size, max_width, minimum) * scale))
        outline = max(4, round(8 * scale))
        rows.append(f"Dialogue: 0,0:00:00.00,{ass_time(duration)},Intro,,0,0,0,,"
                    f"{{\\an5\\pos({round(x*width/1920)},{round(y*height/1080)})"
                    f"\\fs{size}\\bord{outline}\\shad0}}{safe}")
    if structured:
        sizes = structured_intro_sizes(intro)
        # Four equal visual levels. Only the individual field that exceeds its
        # width box shrinks; the ASS script stays explicitly unwrapped.
        add(intro.get("tournament", ""), 960, 270, sizes["tournament"], 1660,
            NORMAL_INTRO_MIN_SIZE)
        add(intro.get("category", ""), 960, 430, sizes["category"], 1660,
            NORMAL_INTRO_MIN_SIZE)
        add(intro.get("playerA", ""), 550, 635, sizes["playerA"], 640,
            NORMAL_INTRO_MIN_SIZE)
        add("VS" if intro.get("playerA") or intro.get("playerB") else "",
            960, 635, 100, 190)
        add(intro.get("playerB", ""), 1370, 635, sizes["playerB"], 640,
            NORMAL_INTRO_MIN_SIZE)
        add(intro.get("schoolA", ""), 550, 795, sizes["schoolA"], 640,
            NORMAL_INTRO_MIN_SIZE)
        add(intro.get("schoolB", ""), 1370, 795, sizes["schoolB"], 640,
            NORMAL_INTRO_MIN_SIZE)
    else:
        # v1 could contain arbitrary freeform matchup/school lines. Never discard them.
        for line, y, size in zip(list(lines)[:4], (285, 430, 635, 775),
                                 (136, 106, 136, 92)):
            add(line, 960, y, size, 1660)
    return header + "\n".join(rows) + "\n"


def thumbnail_path(out):
    return os.path.splitext(out)[0] + ".thumbnail.jpg"


def subtitle_filter(name, font_dir=None):
    escaped = name.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    result = f"subtitles='{escaped}'"
    if font_dir:
        path = font_dir.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        result += f":fontsdir='{path}'"
    return result


def fit_video_filter(width, height):
    """Normal-match geometry: preserve the whole frame and letter/pillar-box it."""
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def fill_video_filter(width, height):
    """Normal-match thumbnail geometry: fill the canvas with a centered crop."""
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1")


def with_intro_filter(match_graph, ass_name, fps, duration, font_dir=None,
                      score_ass_name=None, score_font_dir=None,
                      intro_video="[0:v]", intro_audio="[0:a]"):
    """Place the unscored opener before the existing scored match stream."""
    if score_ass_name and score_font_dir:
        old = subtitle_filter(score_ass_name)
        match_graph = match_graph.replace(old, subtitle_filter(score_ass_name,
                                                               score_font_dir))
    match_graph = match_graph.replace("[vout]", "[matchv]")
    return (match_graph + ";"
            + f"{intro_video}trim=duration={duration:.3f},fps={fps},setpts=N/FRAME_RATE/TB,"
            + subtitle_filter(ass_name, font_dir) + "[introv];"
            + f"{intro_audio}atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[introa];"
            + "[introv][introa][matchv][ac]concat=n=2:v=1:a=1[vout][aout]")


def thumbnail_command(ffmpeg, video, out, ass_name, font_dir=None):
    return [ffmpeg, "-y", "-i", os.path.abspath(video), "-vf",
            fill_video_filter(1280, 720) + "," + subtitle_filter(ass_name, font_dir),
            "-frames:v", "1", "-q:v", "2", os.path.basename(thumbnail_path(out))]
