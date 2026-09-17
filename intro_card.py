"""Optional opening title and matching YouTube thumbnail for ttcut."""

import os
import re
import subprocess

FONT_PREFERENCES = ("Xingkai TC", "Kaiti TC", "Songti TC", "PingFang TC")
SAMPLE_LINES = ("城市盃全國桌球錦標賽", "國小男生二年級團體賽",
                "許宸愷 VS 曾柏誠", "光復國小    吉林國小")


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


def intro_ass(lines, width, height, duration, font):
    """Four separately sized centered lines; ASS scales with the video size."""
    scale = min(width / 1920, height / 1080)
    sizes = (110, 84, 112, 78)
    ys = (310, 435, 600, 725)
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
    for i, line in enumerate(list(lines)[:4]):
        safe = re.sub(r"[{}\\\r\n]", " ", str(line)).strip()
        if not safe:
            continue
        size = max(16, round(sizes[i] * scale))
        y = round(ys[i] * height / 1080)
        outline = max(4, round(8 * scale))
        rows.append(f"Dialogue: 0,0:00:00.00,{ass_time(duration)},Intro,,0,0,0,,"
                    f"{{\\an5\\pos({width//2},{y})\\fs{size}\\bord{outline}\\shad0}}{safe}")
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


def with_intro_filter(match_graph, ass_name, fps, duration, font_dir=None,
                      score_ass_name=None, score_font_dir=None):
    """Place the unscored opener before the existing scored match stream."""
    if score_ass_name and score_font_dir:
        old = subtitle_filter(score_ass_name)
        match_graph = match_graph.replace(old, subtitle_filter(score_ass_name,
                                                               score_font_dir))
    match_graph = match_graph.replace("[vout]", "[matchv]")
    return (match_graph + ";"
            + f"[0:v]trim=duration={duration:.3f},fps={fps},setpts=N/FRAME_RATE/TB,"
            + subtitle_filter(ass_name, font_dir) + "[introv];"
            + f"[0:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[introa];"
            + "[introv][introa][matchv][ac]concat=n=2:v=1:a=1[vout][aout]")


def thumbnail_command(ffmpeg, video, out, ass_name, font_dir=None):
    return [ffmpeg, "-y", "-i", os.path.abspath(video), "-vf",
            "scale=1280:720:force_original_aspect_ratio=increase,"
            "crop=1280:720," + subtitle_filter(ass_name, font_dir),
            "-frames:v", "1", "-q:v", "2", os.path.basename(thumbnail_path(out))]
