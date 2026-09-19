#!/usr/bin/env python3
"""
ttcut V2 — 桌球比賽影片：標記、剪去撿球、疊上常駐計分板，一個工具做完。

版本規則
    小改動 +0.1（V1 → V1.1 → V1.2 …）
    架構或輸出格式的重大變更才進位到 V2

更新紀錄
    V2.3 2026-09-13
        · 新增「數據統計」：勾選後片尾凍結最後一格畫面，疊上置中的統計看板
        · 上半部是各局比分表（名字 → 總局數 → 各局分數），贏的一方用強調色標出
        · 下半部左右分欄，各自列出總得分數、發球得分率、最多連續得分
        · 最多連續得分跨局累計：連兩局 11:0 就是 22 分
        · 停留秒數可調（預設 1 秒），凍結期間音軌補靜音，聲畫長度一致
        · 設定存進標記 JSON 的 stats，命令列另有 --stats / --no-stats / --stats-hold
        · 沒勾選時產出的 .ass 與 filter 與 V2.2 逐字相同
        · 順手修正：Homebrew 沒有 ffmpeg 這個 formula，說明改成 brew install ffmpeg
    V2.2 2026-08-30
        · 加寬數字輸入欄位：緩衝秒數（0.5、1.0）與影格率（29.97）原本會被截掉
    V2.1 2026-08-30
        · 計分板強調色可調：得分數字與名字左側裝飾條連動改色，兩處一起換
        · 顏色存進標記 JSON 的 scoreboard.accent，跟著檔案跨機器
        · 命令列新增 --accent，優先於 JSON 裡的設定
        · 未指定顏色時產出的 .ass 與 V2 逐字相同，其餘顏色不受影響
    V2  2026-08-24
        · 整合成單一工具：直接執行就開伺服器並打開瀏覽器，標記完按鈕即可產出成片
        · 計分邏輯統一由 Python 提供（/fold），tagger 的 JS 版本已移除
          —— 以後改規則只要改一處，不必再做 JS／Python 雙邊交叉驗證
        · 換發球輪次邏輯從 JS 移進 Python，納入同一份 fold()
        · 修正 tagger 與 ttcut 的最短剪點不一致（顯示用 0.15s、實際剪 2.0s）
          現在兩邊都用同一個值，且可在介面上調整
        · 影片改由 Python 提供並支援 HTTP Range，滑桿拖曳與 Safari 播放才正常
        · 影片路徑由 Python 開原生檔案對話框取得（瀏覽器拿不到真實路徑）
        · ffmpeg -progress 進度條，背景執行不卡 UI
        · 保留：匯出／讀入 JSON、命令列渲染路徑，兩者行為與 V1.22 相同
    V1.22 2026-08-20
        · 支援起始局數（一局一支影片、接續前面局數時用）
        · 支援起始分數／讓分，可選每局套用或僅第一局
        · 新增封頂制：10:10 後先到第 12 分者勝，不必贏兩分
        · JSON 新增 format / start 兩個區塊；舊檔缺欄位會自動退回預設值
    V1.2 2026-08-20
        · 修正致命 bug：V1.1 重構時漏掉 -c:v，ffmpeg 一直默默用預設的 libx264
        · 新增 --hwaccel，Mac 預設開 videotoolbox 硬體解碼
        · --hdr keep 遇到非 HDR 片源會自動退回
        · libx264/libx265 給了 --bitrate 就改走碼率模式
    V1.1 2026-08-20
        · 計分板：局數改成「填色底板 + 深色數字」
        · 畫質：影格率跟著片源、碼率依 解析度×影格率 換算、--quality 三檔、HDR 偵測
    V1  2026-08-20  首個可用版本

用法:
    python3 ttcut_v2_3.py                                   ← 開介面（一般用這個）
    python3 ttcut_v2_3.py IMG_1496.tags.json IMG_1496.MOV   ← 命令列直接渲染
    python3 ttcut_v2_3.py tags.json video.MOV --quality max --dry-run

需求:
    Python 3.8+ 與 ffmpeg（本版本不打包，請自行安裝）
    Mac    : brew install ffmpeg     （subtitles 濾鏡需要 libass）
    Windows: 下載 ffmpeg.exe 放在本腳本旁邊，或用 --ffmpeg 指定資料夾
"""

import argparse, errno, json, math, mimetypes, os, platform, re, shutil, socket, unicodedata
import subprocess, sys, tempfile, threading, time, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from intro_card import (FONT_PREFERENCES, installed_families, select_font, intro_duration,
                        intro_ass, intro_has_text, with_intro_filter, thumbnail_command, thumbnail_path,
                        font_directory)
from match_io import (SourceError, natural_paths, make_sources, source_at,
                      reorder_sources, compatibility_issues, geometry_mismatch,
                      output_layout)

try:
    from rally_detection import DetectionError, detect_video
except ImportError:                         # 主程式仍可單獨使用；只停用實驗功能
    DetectionError = RuntimeError
    detect_video = None

VERSION = "V2.3"

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"

# ─────────────────────────────────────────── 版面（以 1920×1080 為基準，會自動縮放）

BASE_W, BASE_H = 1920, 1080
PAD_L, PAD_B   = 64, 64
PANEL_W        = 480
ROW_H          = 54
COL_GAMES_X    = 312          # 局數欄左邊界（相對 panel 左緣）
COL_POINTS_X   = 392          # 得分欄左邊界
FS_NUM         = 44           # 局數與得分共用同一個字級

C_PANEL    = "&H40250A&"      # 深藍底（ASS 是 BGR）
C_ACCENT   = "&H187AFF&"      # 橘 #FF7A18
C_NAME     = "&HF9F2EA&"      # 近白
C_GAMES_BG = "&HEDE3D6&"      # 局數欄底色：亮色塊，跟深底做反差
C_GAMES    = "&H40250A&"      # 局數字：深藍壓在亮底上
C_POINTS   = "&H187AFF&"      # 得分：橘字壓在深底上
C_RULE     = "&H6E4820&"      # 分隔線

A_PANEL, A_CHIP, A_RULE = 0x1E, 0x00, 0x40    # 0x00 全不透明 → 0xFF 全透明

DEFAULT_ACCENT = "#FF7A18"    # 得分數字與名字左側裝飾條共用的強調色

# Koko 彩色表格；基準為 1920×1080，與原版共用等比縮放係數。
KOKO_NAME_MIN_W, KOKO_NAME_MAX_W = 240, 510
KOKO_GAMES_W, KOKO_POINTS_W = 78, 92
KOKO_ROW_H = 60
KOKO_NAME_PAD = 24
KOKO_NAME_FS, KOKO_NAME_MIN_FS = 34, 22
KOKO_NUM_FS = 42
KOKO_NAME_BG = "&H201810&"     # #101820，深色半透明
KOKO_GAMES_BG = "&H9B5519&"    # #19559B，藍
KOKO_POINTS_BG = "&H3F7014&"   # #14703F，綠
KOKO_WHITE = "&HFFFFFF&"
KOKO_NAME_ALPHA = 0x20


C_DIM      = "&HCEB28F&"      # 次要文字：偏暗的藍灰

# ─────────────────────────────────────────── 數據統計看板（同樣以 1920×1080 為基準）

SB_PAD         = 54           # 看板內距
SB_RULE        = 2            # 分隔線粗細
SB_HEAD_H      = 42           # 比分表的 局數／比分 表頭列高
SB_ROW_H       = 86           # 比分表每位選手一列
SB_GAP         = 36           # 比分表與統計區之間的留白
SB_TITLE_H     = 66           # 統計區標題列高
SB_NAMES_H     = 54           # 統計區的左右選手名列高
SB_STAT_ROW_H  = 78           # 統計區每個指標一列
SB_NAME_W      = 420          # 比分表名字欄的保留寬度
SB_GAMES_W     = 124          # 總局數欄寬（跟轉播一樣放在各局比分前面）
SB_COL_W       = 74           # 每一局的欄寬，局數多時會自動縮
SB_MIN_W       = 1180         # 看板最小寬度
A_BOARD        = 0x0C         # 看板底色比角落計分板更實，數字才讀得清楚

DEFAULT_STATS_HOLD = 1.0      # 片尾停留秒數

STATS_TITLE  = "數據統計"
STATS_GAMES  = "局數"
STATS_PTS    = "各局比分"
STATS_LABELS = ("總得分數", "發球得分率", "最多連續得分")


def ass_colour(hex_rgb, fallback=C_ACCENT):
    """把 #RRGGBB 轉成 ASS 的 &HBBGGRR&。ASS 是 BGR 順序，寫反了顏色會整個跑掉。"""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (hex_rgb or "").strip())
    if not m:
        return fallback
    s = m.group(1).upper()
    return f"&H{s[4:6]}{s[2:4]}{s[0:2]}&"


def ass_text(s):
    """ASS 沒有跳脫字元，大括號與反斜線會被當成標籤起頭，選手名含這些字時直接換掉。"""
    return (str(s).replace("\\", "/").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())

# 各平台實際裝得到的中文字型
FONT_NAME = ("PingFang TC" if IS_MAC else
             "Microsoft JhengHei" if IS_WIN else "Noto Sans CJK TC")
FONT_NUM  = ("Helvetica Neue" if IS_MAC else
             "Segoe UI" if IS_WIN else "DejaVu Sans")

# 各平台的硬體編碼器
HW_ENCODER = "h264_videotoolbox" if IS_MAC else "libx264"

# --quality 三檔：碼率倍率 / CRF / x264 preset / 是否強制走純軟體編碼
QUALITY = {
    "fast": dict(scale=0.70, crf=21, preset="veryfast", force_sw=False),
    "high": dict(scale=1.00, crf=18, preset="medium",   force_sw=False),
    "max":  dict(scale=1.40, crf=16, preset="slow",     force_sw=True),
}

DEFAULT_MIN_CUT = 2.5         # 短於此秒數就不剪，避免無意義的跳接


# ─────────────────────────────────────────── 比分推導（唯一權威版本）

def fold_full(events, fmt, start, first_server=0):
    """把事件流摺成計分狀態。這是全專案唯一的計分實作，介面與命令列共用。

    fmt   = dict(target, deuce='standard'|'capped', cap)
    start = dict(games=(gA, gB), points=(a, b), scope='every'|'first')
    first_server = 0(A) / 1(B)

    回傳 dict:
        states  [(來源時間, gA, gB, a, b), ...]   給 ASS 計分板用，第一筆時間為 None
        snaps   與 events 等長；point 事件給 dict，其餘為 None，給介面事件列表用
        cur     目前狀態，含下一球該誰發

    換發球以「實際打過的分數」計算，讓分的起始分不計入輪次；
    雙方都到達 target-1 之後（deuce）改成每分換發。
    """
    T, mode, cap = fmt["target"], fmt["deuce"], fmt["cap"]
    gA, gB = start["games"]
    sp, scope = list(start["points"]), start["scope"]

    def init_pts(gi):
        return list(sp) if (scope == "every" or gi == 0) else [0, 0]

    gi = 0
    a, b = init_pts(0)
    server, served_in_turn = first_server, 0
    pending = False                        # 局末：先把比分留在畫面上，下一分才歸零
    states = [(None, gA, gB, a, b)]        # 開頭狀態，時間稍後補
    snaps = []
    plays = []                             # 每一分的細節，供片尾統計看板使用

    def is_deuce():
        return a >= T - 1 and b >= T - 1

    def game_over():
        hi, lo = max(a, b), min(a, b)
        if mode == "capped" and hi >= cap:      # 10:10 後先到 cap 者勝，不必贏兩分
            return True
        return hi >= T and hi - lo >= 2          # 標準：11 分且領先 2 分

    for e in events:
        if e["type"] == "game":
            gi += 1
            a, b = init_pts(gi)
            server, served_in_turn = (first_server + gi) % 2, 0
            pending = False
            states.append((e["t"], gA, gB, a, b))
            snaps.append(None)
            continue
        if e["type"] != "point":
            snaps.append(None)
            continue
        if pending:
            gi += 1
            a, b = init_pts(gi)
            pending = False
        srv = server                       # 這一分是誰發的球（輪換在本次迴圈稍後才更新）
        if e["winner"] == "A":
            a += 1
        else:
            b += 1
        won = game_over()
        snaps.append(dict(a=a, b=b, won=won,
                          gA=gA + (1 if won and a > b else 0),
                          gB=gB + (1 if won and b > a else 0)))
        plays.append(dict(t=e["t"], gi=gi, winner=0 if e["winner"] == "A" else 1,
                          server=srv, a=a, b=b, won=won))
        if won:
            if a > b:
                gA += 1
            else:
                gB += 1
            pending = True
            server, served_in_turn = (first_server + gi + 1) % 2, 0   # 下一局換人先發
        else:
            served_in_turn += 1
            if served_in_turn >= (1 if is_deuce() else 2):
                server, served_in_turn = 1 - server, 0
        states.append((e["t"], gA, gB, a, b))

    cur = dict(a=a, b=b, gA=gA, gB=gB, gi=gi, server=server, pending=pending,
               gameNo=gA + gB + 1 - (1 if pending else 0))
    return dict(states=states, snaps=snaps, plays=plays, cur=cur)


def fold(events, fmt, start):
    """給 ASS 計分板用的狀態序列（與 V1.22 的 fold() 輸出完全相同）。"""
    return fold_full(events, fmt, start)["states"]


def read_format(doc):
    """從 JSON 讀賽制與起始比分，舊檔缺欄位時退回預設值。"""
    f = doc.get("format", {}) or {}
    target = int(f.get("pointsPerGame", doc.get("pointsPerGame", 11)))
    mode = f.get("deuce", "standard")
    cap = int(f.get("cap", target + 1))
    st = doc.get("start", {}) or {}
    g = st.get("games", {}) or {}
    p = st.get("points", {}) or {}
    fmt = dict(target=target, deuce=mode, cap=cap)
    start = dict(games=[int(g.get("A", 0)), int(g.get("B", 0))],
                 points=[int(p.get("A", 0)), int(p.get("B", 0))],
                 scope=st.get("handicapScope", "every"))
    return fmt, start


def _better(x, y):
    """回傳數值較大的那一邊（0/1），一樣大回 None。"""
    return 0 if x > y else 1 if y > x else None


def match_stats(sc, start, players):
    """把 fold 的結果整理成片尾統計看板要的數字。

    games     每一局的比分與勝方；手動換局造成的空局以 None 補位
    points    實際贏下的分數（讓分的起始分不算）
    serveRate 自己發球時的得分率＝發球局贏的分 ÷ 發球的分
    streak    最長連續得分，跨局累計（連兩局 11:0 就是 22）
    """
    plays = sc["plays"]
    off = start["games"][0] + start["games"][1]

    last_gi = max((p["gi"] for p in plays), default=-1)
    games = []
    for gi in range(last_gi + 1):
        ps = [p for p in plays if p["gi"] == gi]
        if ps:
            lp = ps[-1]
            games.append(dict(no=off + gi + 1, a=lp["a"], b=lp["b"], done=lp["won"],
                              winner=(0 if lp["a"] > lp["b"] else 1) if lp["won"] else None))
        else:
            games.append(dict(no=off + gi + 1, a=None, b=None, done=False, winner=None))

    points = [sum(1 for p in plays if p["winner"] == i) for i in (0, 1)]
    served = [sum(1 for p in plays if p["server"] == i) for i in (0, 1)]
    won_srv = [sum(1 for p in plays if p["server"] == i and p["winner"] == i)
               for i in (0, 1)]
    rate = [round(won_srv[i] / served[i], 4) if served[i] else None for i in (0, 1)]

    streak, who, run = [0, 0], None, 0
    for p in plays:
        run = run + 1 if p["winner"] == who else 1
        who = p["winner"]
        streak[who] = max(streak[who], run)

    return dict(players=[str(players[0]), str(players[1])], games=games,
                gamesWon=[sc["cur"]["gA"], sc["cur"]["gB"]],
                points=points, served=served, servedWon=won_srv,
                serveRate=rate, streak=streak)


# ─────────────────────────────────────────── 剪接區間

def build_cuts(events, tail, lead, min_cut, cut_lets, let_tail):
    """得分→下次發球 = 剪。發球→發球（重發）= 預設保留，可選擇也剪。"""
    cuts = []
    for i, e in enumerate(events):
        if e["type"] == "point":
            nxt = next((x for x in events[i + 1:] if x["type"] == "serve"), None)
            if nxt:
                cuts.append((e["t"] + tail, nxt["t"] - lead, "得分後撿球"))
        elif e["type"] == "serve" and cut_lets:
            nxt = events[i + 1] if i + 1 < len(events) else None
            if nxt and nxt["type"] == "serve":
                cuts.append((e["t"] + let_tail, nxt["t"] - lead, "重發後撿球"))

    kept, dropped = [], []
    for f, t, why in cuts:
        (kept if t - f >= min_cut else dropped).append((f, t, why))
    return sorted(kept), sorted(dropped)


def keeps_from_cuts(head, end, cuts):
    """剪點的補集 = 要保留的片段。"""
    segs, cur = [], head
    for f, t, _ in cuts:
        if f > cur:
            segs.append((cur, min(f, end)))
        cur = max(cur, t)
        if cur >= end:
            break
    if cur < end:
        segs.append((cur, end))
    return [(s, e) for s, e in segs if e - s > 0.04]


def make_mapper(keeps):
    """來源時間 → 成片時間。落在剪掉區間內的時間點會貼到下一段的起點。"""
    acc, table = 0.0, []
    for s, e in keeps:
        table.append((s, e, acc))
        acc += e - s
    total = acc

    def src2out(t):
        for s, e, base in table:
            if t < s:
                return base
            if t <= e:
                return base + (t - s)
        return total

    return src2out, total


# ─────────────────────────────────────────── ASS 計分板

def ts(t):
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def rect(x, y, w, h, colour, alpha, layer=0):
    """實心矩形。注意 \\alpha 必須寫在 \\1a 前面，否則會把 \\1a 蓋掉。"""
    tags = (f"\\an7\\pos({x},{y})\\p1\\bord0\\shad0"
            f"\\alpha&H00&\\1c{colour}\\1a&H{alpha:02X}&")
    return layer, f"{{{tags}}}m 0 0 l {w} 0 l {w} {h} l 0 {h}"


def koko_name_units(name):
    """估算粗體中文字型的 em 寬度：全形/CJK 為 1，拉丁字元為 0.58。"""
    return sum(0 if unicodedata.combining(ch) else
               1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.58
               for ch in ass_text(name))


def koko_name_width(names):
    """兩列共用的 1080p 姓名欄寬；依較長標籤計算並限制上下限。"""
    units = max((koko_name_units(name) for name in names), default=0)
    natural = math.ceil(units * KOKO_NAME_FS + 2 * KOKO_NAME_PAD)
    return max(KOKO_NAME_MIN_W, min(KOKO_NAME_MAX_W, natural))


def koko_layout(width, height, names=()):
    """Koko 欄位幾何，以 1080p 座標等比縮放到輸出解析度。"""
    k = min(width / BASE_W, height / BASE_H)
    s = lambda value: round(value * k)
    base_name_w = koko_name_width(names)
    name_w, games_w, points_w = map(
        s, (base_name_w, KOKO_GAMES_W, KOKO_POINTS_W))
    row_h = s(KOKO_ROW_H)
    x = s(PAD_L)
    y = height - s(PAD_B) - 2 * row_h
    return dict(k=k, x=x, y=y, base_name_w=base_name_w,
                name_w=name_w, games_w=games_w,
                points_w=points_w, row_h=row_h,
                games_cx=x + name_w + games_w // 2,
                points_cx=x + name_w + games_w + points_w // 2)


def koko_name_size(name, name_width=KOKO_NAME_MAX_W):
    """只縮姓名字級；達最大欄寬後縮字，最小字級仍放不下則 clip。"""
    units = koko_name_units(name)
    available = name_width - 2 * KOKO_NAME_PAD
    return max(KOKO_NAME_MIN_FS,
               min(KOKO_NAME_FS, int(available / max(units, 1))))


def koko_scoreboard_lines(stamped, total, names, width, height):
    """兩列對齊表格；每項為 (layer, style, start, end, ASS text)。"""
    geo = koko_layout(width, height, names)
    k, x, y = geo["k"], geo["x"], geo["y"]
    nw, gw, pw, rh = (geo[key] for key in
                       ("name_w", "games_w", "points_w", "row_h"))
    s = lambda value: round(value * k)
    total_w = nw + gw + pw
    out = []
    for row in (0, 1):
        top = y + row * rh
        for left, cell_w, colour, alpha in (
                (x, nw, KOKO_NAME_BG, KOKO_NAME_ALPHA),
                (x + nw, gw, KOKO_GAMES_BG, 0),
                (x + nw + gw, pw, KOKO_POINTS_BG, 0)):
            out.append((0, "Gfx", 0, total,
                        rect(left, top, cell_w, rh, colour, alpha)[1]))
        name_x = x + s(KOKO_NAME_PAD)
        name_y = top + rh // 2
        clip_right = x + nw - s(KOKO_NAME_PAD)
        fs = s(koko_name_size(names[row], geo["base_name_w"]))
        out.append((1, "Nm", 0, total,
                    f"{{\\an4\\pos({name_x},{name_y})\\fs{fs}\\b1"
                    f"\\1c{KOKO_WHITE}\\clip({name_x},{top},{clip_right},{top + rh})}}"
                    f"{ass_text(names[row])}"))
    # 細分隔線固定在欄位邊界，避免文字或雙位數改變幾何。
    rule = max(1, s(2))
    for bx, by, bw, bh in ((x, y + rh, total_w, rule),
                           (x + nw, y, rule, 2 * rh),
                           (x + nw + gw, y, rule, 2 * rh)):
        out.append((1, "Gfx", 0, total,
                    rect(bx, by, bw, bh, KOKO_NAME_BG, 0)[1]))
    fs_num = s(KOKO_NUM_FS)
    for i, (start, gA, gB, a, b) in enumerate(stamped):
        end = stamped[i + 1][0] if i + 1 < len(stamped) else total
        if end - start < 0.02:
            continue
        for row, (games, points) in enumerate(((gA, a), (gB, b))):
            cy = y + row * rh + rh // 2
            out.append((2, "Nu", start, end,
                        f"{{\\an5\\pos({geo['games_cx']},{cy})\\fs{fs_num}"
                        f"\\b1\\1c{KOKO_WHITE}}}{games}"))
            out.append((2, "Nu", start, end,
                        f"{{\\an5\\pos({geo['points_cx']},{cy})\\fs{fs_num}"
                        f"\\b1\\1c{KOKO_WHITE}}}{points}"))
    return out


def stats_lines(stats, k, width, height, c_accent):
    """片尾數據統計看板，回傳 (layer, style, 文字) 的串列。

    上半部是各局比分表：名字 → 總局數 → 各局分數，每局贏的數字用亮色、輸的壓暗；
    下半部是左右對照，左邊數字是 A、右邊是 B，指標名稱置中，較好的一邊用強調色。"""
    S = lambda v: round(v * k)
    DASH, EMDASH = "\u2013", "\u2014"
    out = []

    def box(x, y, w, h, colour, alpha):
        out.append((0, "Gfx", rect(x, y, w, h, colour, alpha)[1]))

    def txt(layer, style, body):
        out.append((layer, style, body))

    games = stats["games"] or [dict(no=1, a=None, b=None, done=False, winner=None)]
    n = len(games)
    room = BASE_W - 160 - 2 * SB_PAD - SB_NAME_W - SB_GAMES_W
    col_w = min(SB_COL_W, max(40, room // n))
    blk = SB_GAMES_W + n * col_w
    bw = max(SB_MIN_W, 2 * SB_PAD + SB_NAME_W + blk)
    bh = (2 * SB_PAD + SB_HEAD_H + 2 * SB_ROW_H + 3 * SB_RULE + SB_GAP
          + SB_TITLE_H + SB_NAMES_H + 3 * (SB_RULE + SB_STAT_ROW_H))

    pw, ph = S(bw), S(bh)
    x0, y0 = (width - pw) // 2, (height - ph) // 2
    pad = S(SB_PAD)
    lx, rx = x0 + pad, x0 + pw - pad              # 內容的左右邊界
    cx, cw = x0 + pw // 2, pw - 2 * pad
    rule_h = max(1, S(SB_RULE))

    def rule(y):
        box(lx, y, cw, rule_h, C_RULE, A_RULE)

    box(x0, y0, pw, ph, C_PANEL, A_BOARD)
    box(x0, y0, S(6), ph, c_accent, 0x00)

    # 上半部：各局比分表
    y = y0 + pad
    gx = rx - S(blk)
    gcx = gx + S(SB_GAMES_W) // 2
    ccx = lambda i: gx + S(SB_GAMES_W) + i * S(col_w) + S(col_w) // 2
    pcx = gx + S(SB_GAMES_W) + S(n * col_w) // 2

    hcy = y + S(SB_HEAD_H) // 2
    txt(1, "Nm", f"{{\\an5\\pos({gcx},{hcy})\\fs{S(20)}\\fsp{S(3)}"
                 f"\\1c{C_DIM}}}{ass_text(STATS_GAMES)}")
    txt(1, "Nm", f"{{\\an5\\pos({pcx},{hcy})\\fs{S(20)}\\fsp{S(3)}"
                 f"\\1c{C_DIM}}}{ass_text(STATS_PTS)}")
    y += S(SB_HEAD_H)

    lead = _better(stats["gamesWon"][0], stats["gamesWon"][1])
    for r in (0, 1):
        rule(y)
        y += rule_h
        rcy = y + S(SB_ROW_H) // 2
        txt(1, "Nm", f"{{\\an4\\pos({lx},{rcy})\\fs{S(36)}\\b1"
                     f"\\1c{C_NAME}}}{ass_text(stats['players'][r])}")
        txt(2, "Nu", f"{{\\an5\\pos({gcx},{rcy})\\fs{S(46)}\\b1"
                     f"\\1c{c_accent if lead == r else C_NAME}}}{stats['gamesWon'][r]}")
        for i, g in enumerate(games):
            v = g["a"] if r == 0 else g["b"]
            cell = DASH if v is None else str(v)
            txt(2, "Nu", f"{{\\an5\\pos({ccx(i)},{rcy})\\fs{S(30)}\\b1"
                         f"\\1c{C_NAME if g['winner'] == r else C_DIM}}}{cell}")
        y += S(SB_ROW_H)
    rule(y)
    y += rule_h + S(SB_GAP)

    # 下半部：左右對照的個人指標
    txt(1, "Nm", f"{{\\an4\\pos({lx},{y + S(SB_TITLE_H) // 2})\\fs{S(38)}"
                 f"\\b1\\fsp{S(2)}\\1c{C_NAME}}}{ass_text(STATS_TITLE)}")
    y += S(SB_TITLE_H)

    ncy = y + S(SB_NAMES_H) // 2
    for side, (ax, al) in enumerate(((lx, 4), (rx, 6))):
        txt(1, "Nm", f"{{\\an{al}\\pos({ax},{ncy})\\fs{S(26)}\\b1\\fsp{S(3)}"
                     f"\\1c{C_NAME}}}{ass_text(stats['players'][side])}")
    y += S(SB_NAMES_H)

    pts, rate, streak = stats["points"], stats["serveRate"], stats["streak"]
    pct = lambda i: EMDASH if rate[i] is None else f"{round(rate[i] * 100)}%"
    shown = [-1 if rate[i] is None else round(rate[i] * 100) for i in (0, 1)]
    sub = lambda i: (f"{stats['servedWon'][i]}/{stats['served'][i]}"
                     if stats["served"][i] else "")
    rows = [(str(pts[0]), str(pts[1]), _better(pts[0], pts[1]), "", ""),
            (pct(0), pct(1), _better(shown[0], shown[1]), sub(0), sub(1)),
            (str(streak[0]), str(streak[1]), _better(streak[0], streak[1]), "", "")]

    small = f"\\fs{S(19)}\\b0\\1c{C_DIM}"
    for j, (va, vb, best, sa, sb) in enumerate(rows):
        rule(y)
        y += rule_h
        scy = y + S(SB_STAT_ROW_H) // 2
        txt(1, "Nm", f"{{\\an5\\pos({cx},{scy})\\fs{S(29)}"
                     f"\\1c{C_NAME}}}{ass_text(STATS_LABELS[j])}")
        ca = c_accent if best == 0 else C_NAME
        cb = c_accent if best == 1 else C_NAME
        txt(2, "Nu", f"{{\\an4\\pos({lx},{scy})\\fs{S(44)}\\b1\\1c{ca}}}{va}"
                     + (f"{{{small}}}  {sa}" if sa else ""))
        txt(2, "Nu", f"{{\\an6\\pos({rx},{scy})}}"
                     + (f"{{{small}}}{sb}  " if sb else "")
                     + f"{{\\fs{S(44)}\\b1\\1c{cb}}}{vb}")
        y += S(SB_STAT_ROW_H)
    return out


def build_ass(states, src2out, total, names, width, height,
              font_name=FONT_NAME, font_num=FONT_NUM, accent=None,
              stats=None, hold=0.0, scoreboard_style="ttcut"):
    # 得分數字與名字左側的裝飾條共用同一個色，一起換
    c_accent = c_points = accent or C_ACCENT
    k = min(width / BASE_W, height / BASE_H)
    S = lambda v: round(v * k)                        # 縮放
    x0 = S(PAD_L)
    y0 = height - S(PAD_B) - S(ROW_H * 2)
    pw, rh = S(PANEL_W), S(ROW_H)
    row_y = (y0 + rh // 2, y0 + rh + rh // 2)
    name_x = x0 + S(28)
    gx, gw = x0 + S(COL_GAMES_X), S(COL_POINTS_X) - S(COL_GAMES_X)
    games_cx = gx + gw // 2
    points_cx = x0 + S(COL_POINTS_X) + (pw - S(COL_POINTS_X)) // 2
    fs = S(FS_NUM)

    head = f"""[Script Info]
; ttcut {VERSION}
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Nm,{font_name},{S(29)},{C_NAME},{C_NAME},&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1
Style: Nu,{font_num},{fs},{c_points},{c_points},&H00000000&,&H00000000&,1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: Gfx,Arial,20,&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []
    add = lambda layer, style, a, b, txt: lines.append(
        f"Dialogue: {layer},{ts(a)},{ts(b)},{style},,0,0,0,,{txt}")

    stamped = [(0.0, *states[0][1:])] if states[0][0] is None else []
    stamped += [(src2out(t), gA, gB, a, b) for t, gA, gB, a, b in states if t is not None]

    if scoreboard_style not in ("ttcut", "koko"):
        raise ValueError(f"未知比分板樣式：{scoreboard_style}")
    if scoreboard_style == "koko":
        for layer, style, start, end, body in koko_scoreboard_lines(
                stamped, total, names, width, height):
            add(layer, style, start, end, body)
    else:
        # 原版 ASS 分支保留原有畫法。
        # ── 底板、局數色塊、橘色側邊、分隔線（整片常駐）
        # 同一 layer 內依出現順序疊，所以分隔線放最後才會壓在色塊上面
        for layer, d in [
            rect(x0, y0, pw, rh * 2, C_PANEL, A_PANEL),                    # 底板
            rect(gx, y0, gw, rh, C_GAMES_BG, A_CHIP),                      # 局數色塊（上）
            rect(gx, y0 + rh, gw, rh, C_GAMES_BG, A_CHIP),                 # 局數色塊（下）
            rect(x0, y0, S(5), rh * 2, c_accent, 0x00),                    # 側邊裝飾條
            rect(x0, y0 + rh, pw, max(1, S(2)), C_RULE, A_RULE),           # 橫向分隔
            rect(gx, y0, max(1, S(2)), rh * 2, C_RULE, A_RULE),
            rect(x0 + S(COL_POINTS_X), y0, max(1, S(2)), rh * 2, C_RULE, A_RULE),
        ]:
            add(layer, "Gfx", 0, total, d)

        # ── 選手名（常駐）
        for i, nm in enumerate(names):
            add(1, "Nm", 0, total,
                f"{{\\an4\\pos({name_x},{row_y[i]})\\1c{C_NAME}}}{ass_text(nm)}")

        # ── 局數與該局得分（隨事件變動）：同字級、同字重，靠底色分辨
        for i, (t, gA, gB, a, b) in enumerate(stamped):
            end = stamped[i + 1][0] if i + 1 < len(stamped) else total
            if end - t < 0.02:
                continue
            for row, (g, p) in enumerate(((gA, a), (gB, b))):
                add(2, "Nu", t, end,
                    f"{{\\an5\\pos({games_cx},{row_y[row]})\\fs{fs}\\b1\\1c{C_GAMES}}}{g}")
                add(2, "Nu", t, end,
                    f"{{\\an5\\pos({points_cx},{row_y[row]})\\fs{fs}\\b1\\1c{c_points}}}{p}")

    # 片尾統計看板：角落計分板在 total 就結束，看板接著獨占畫面
    if stats and hold > 0:
        for layer, style, body in stats_lines(stats, k, width, height, c_accent):
            add(layer, style, total, total + hold, body)

    return head + "\n".join(lines) + "\n"


# ─────────────────────────────────────────── ffmpeg

def find_ffmpeg(explicit, *hint_dirs):
    """Prefer the macOS libass build, then use the existing search order."""
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    if explicit:
        p = os.path.abspath(explicit)
        if os.path.isdir(p):
            p = os.path.join(p, exe)
        return p if os.path.isfile(p) else None
    if IS_MAC:
        full = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
        if os.path.isfile(full):
            return full
    found = shutil.which("ffmpeg")
    if found:
        return found
    cands = [os.path.dirname(os.path.abspath(__file__)), *hint_dirs]
    if IS_WIN:
        cands += [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                  os.path.expanduser(r"~\ffmpeg\bin"),
                  os.path.expanduser(r"~\scoop\shims")]
    for d in cands:
        if not d:
            continue
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return None


def probe(path, ff="ffprobe"):
    """回傳片源規格 dict，讀不到就回 None。"""
    try:
        out = subprocess.run(
            [ff, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate,r_frame_rate,pix_fmt,codec_name,"
             "color_transfer,color_primaries,bit_rate:format=bit_rate,duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True).stdout
        d = json.loads(out)
        s = d["streams"][0]

        def rate(x):
            try:
                n, den = str(x).split("/")
                return float(n) / float(den) if float(den) else 0.0
            except Exception:
                return 0.0

        frac = s.get("avg_frame_rate") or "0/0"
        if rate(frac) <= 0:
            frac = s.get("r_frame_rate") or "0/0"
        br = s.get("bit_rate") or d.get("format", {}).get("bit_rate")
        dur = d.get("format", {}).get("duration")
        return {
            "w": int(s["width"]), "h": int(s["height"]),
            "fps": rate(frac), "fps_frac": frac if rate(frac) > 0 else None,
            "pix_fmt": s.get("pix_fmt", ""), "codec": s.get("codec_name", ""),
            "trc": s.get("color_transfer", ""), "prim": s.get("color_primaries", ""),
            "bitrate": int(br) if br and str(br).isdigit() else None,
            "duration": float(dur) if dur else None,
        }
    except Exception:
        return None


def probe_audio(path, ff="ffprobe"):
    try:
        raw = subprocess.run(
            [ff, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels,channel_layout", "-of", "json", path],
            capture_output=True, text=True, check=True).stdout
        streams = json.loads(raw).get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        return {key: stream.get(key) for key in
                ("codec_name", "sample_rate", "channels", "channel_layout")}
    except Exception:
        return None


def is_hdr(info):
    return bool(info) and info.get("trc") in ("arib-std-b67", "smpte2084")


def is_10bit(info):
    pf = (info or {}).get("pix_fmt", "")
    return "10" in pf or "p010" in pf or "12" in pf


def auto_bitrate(w, h, fps, scale=1.0):
    """依像素率換算碼率。桌球是高動態畫面，抓得比一般影片寬。"""
    mpix_s = w * h * max(fps, 1) / 1e6          # 1080p30 ≈ 62 Mpix/s
    mbps = mpix_s * 0.30 * scale                # → 約 19 Mbps
    return f"{max(8.0, min(120.0, mbps)):.0f}M"


def video_encoder_args(enc, crf, preset, bitrate, pix_fmt, use_bitrate=False):
    """不同編碼器的品質參數長得都不一樣，這裡統一翻譯。
    注意第一組一定是 -c:v——V1.1 就是漏了它，害 ffmpeg 默默退回預設的 libx264。"""
    c = ["-c:v", enc]
    if enc in ("libx264", "libx265"):
        q = (["-b:v", bitrate, "-maxrate", bitrate,
              "-bufsize", f"{float(bitrate[:-1]) * 2:.0f}M"] if use_bitrate
             else ["-crf", str(crf)])
        return c + ["-preset", preset, *q, "-pix_fmt", pix_fmt]
    if "videotoolbox" in enc:      # VideoToolbox 沒有 CRF，只能給碼率
        return c + ["-b:v", bitrate, "-maxrate", bitrate,
                    "-bufsize", f"{float(bitrate[:-1]) * 2:.0f}M", "-pix_fmt", pix_fmt]
    if "nvenc" in enc:
        return c + ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(crf),
                    "-b:v", "0", "-maxrate", bitrate, "-pix_fmt", pix_fmt]
    if "qsv" in enc:
        return c + ["-preset", "veryslow", "-global_quality", str(crf), "-pix_fmt", pix_fmt]
    if "amf" in enc:
        return c + ["-quality", "quality", "-rc", "cqp",
                    "-qp_i", str(crf), "-qp_p", str(crf), "-pix_fmt", pix_fmt]
    return c + ["-b:v", bitrate, "-pix_fmt", pix_fmt]


TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def filter_script(keeps, ass_name, fps, tonemap=False, hold=0.0,
                  source_video="[0:v]", source_audio="[0:a]"):
    """用 select 串流篩選，而不是 trim+concat——後者會把整段解碼結果緩衝在記憶體裡。
    先 fps 強制固定影格率，避免 iPhone VFR 造成聲畫不同步。
    tone-map 放在字幕之前，計分板顏色才不會被一起壓縮動態範圍。"""
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keeps)
    esc = ass_name.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    v = [f"{source_video}fps={fps}", f"select='{expr}'", "setpts=N/FRAME_RATE/TB"]
    if tonemap:
        v.append(TONEMAP)
    a = [f"{source_audio}aselect='{expr}'", "asetpts=N/SR/TB"]
    if hold > 0:                        # 凍結最後一格，音軌補等長靜音，免得聲畫長度對不上
        v.append(f"tpad=stop_mode=clone:stop_duration={hold:.3f}")
        a.append(f"apad=pad_dur={hold:.3f}")
    return "".join([
        ",".join(v) + "[vc];",
        ",".join(a) + "[ac];",
        f"[vc]subtitles='{esc}'[vout]",
    ])


def human_bitrate(b):
    return f"{b / 1e6:.1f} Mbps" if b else "未知"


# ─────────────────────────────────────────── 規劃（介面與命令列共用）

class PlanError(Exception):
    pass


def plan(doc, opt):
    """從標記 JSON 算出剪接計畫。不碰 ffmpeg，介面即時預覽也用這個。"""
    events = sorted(doc.get("events", []), key=lambda e: e["t"])
    pads = doc.get("pads", {}) or {}
    lead = opt.get("lead") if opt.get("lead") is not None else pads.get("lead", 0.8)
    tail = opt.get("tail") if opt.get("tail") is not None else pads.get("tail", 2.0)
    min_cut = opt.get("min_cut", DEFAULT_MIN_CUT)
    cut_lets = bool(opt.get("cut_lets", False))
    let_tail = opt.get("let_tail", 1.5)
    fmt, start = read_format(doc)
    first_server = 1 if doc.get("firstServer") == "B" else 0

    sc = fold_full(events, fmt, start, first_server)

    # 數據統計：命令列的設定優先，其次讀標記 JSON
    sb = doc.get("stats", {}) or {}
    stats_on = (bool(sb.get("enabled", False)) if opt.get("stats") is None
                else bool(opt.get("stats")))
    raw_hold = (sb.get("hold", DEFAULT_STATS_HOLD) if opt.get("stats_hold") is None
                else opt.get("stats_hold"))
    try:
        hold = max(0.0, min(30.0, float(raw_hold)))
    except (TypeError, ValueError):
        hold = DEFAULT_STATS_HOLD
    if not stats_on:
        hold = 0.0
    players = [doc.get("players", {}).get("A") or "A",
               doc.get("players", {}).get("B") or "B"]
    stats = match_stats(sc, start, players)

    serves = [e for e in events if e["type"] == "serve"]
    points = [e for e in events if e["type"] == "point"]
    if not serves or not points:
        return dict(ok=False, reason="事件裡沒有發球或得分，無法剪接。",
                    scoring=sc, events=events, fmt=fmt, start=start,
                    stats=stats, stats_on=stats_on, hold=0.0,
                    cuts=[], dropped=[], keeps=[], total=0.0, span=0.0)

    head = serves[0]["t"] - lead
    end = points[-1]["t"] + tail
    cuts, dropped = build_cuts(events, tail, lead, min_cut, cut_lets, let_tail)
    keeps = keeps_from_cuts(head, end, cuts)
    src2out, total = make_mapper(keeps)

    return dict(ok=True, reason=None, scoring=sc, events=events,
                fmt=fmt, start=start, lead=lead, tail=tail,
                cuts=cuts, dropped=dropped, keeps=keeps,
                src2out=src2out, total=total,
                stats=stats, stats_on=stats_on, hold=hold,
                head=head, end=end, span=end - head,
                serves=len(serves), points=len(points))


def scoreboard_style_for(doc, opt):
    """新標記保存樣式；沒有 style 的舊標記沿用 ttcut 原版。"""
    style = (opt.get("scoreboard_style")
             or (doc.get("scoreboard", {}) or {}).get("style")
             or "ttcut")
    if style not in ("ttcut", "koko"):
        raise ValueError(f"未知比分板樣式：{style}")
    return style


def multi_filter_graph(sources, w, h, fps, intro=False, mixed_hdr=False, pixel_format="yuv420p"):
    """Join decoded segments in one FFmpeg run; normalize only in the render graph."""
    parts = []
    labels = []
    for i, source in enumerate(sources):
        duration = source["duration"]
        video_filters = [f"trim=duration={duration:.6f}", "setpts=PTS-STARTPTS",
                         f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1",
                         f"fps={fps}"]
        if mixed_hdr and source.get("trc") in ("arib-std-b67", "smpte2084"):
            video_filters.append(TONEMAP)
        video_filters.append(f"format={pixel_format}")
        parts.append(f"[{i}:v]" + ",".join(video_filters) + f"[segv{i}]")
        if source.get("audio"):
            parts.append(f"[{i}:a]atrim=duration={duration:.6f},"
                         "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"asetpts=PTS-STARTPTS[sega{i}]")
        else:
            parts.append("anullsrc=channel_layout=stereo:sample_rate=48000,"
                         f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[sega{i}]")
        labels.append(f"[segv{i}][sega{i}]")
    parts.append("".join(labels) + f"concat=n={len(sources)}:v=1:a=1[virtualv][virtuala]")
    if intro:
        parts += ["[virtualv]split=2[matchsrcv][introsrcv]",
                  "[virtuala]asplit=2[matchsrca][introsrca]"]
        return ";".join(parts) + ";", ("[matchsrcv]", "[matchsrca]",
                                          "[introsrcv]", "[introsrca]")
    return ";".join(parts) + ";", ("[virtualv]", "[virtuala]", None, None)


def build_render(doc, plan_d, video, out, opt, ffmpeg, ffprobe, log=print,
                 progress=False, sources=None, workdir_override=None):
    """寫出 .ass 與 filter，組出 ffmpeg 指令。回傳 (cmd, workdir, info)。"""
    q = QUALITY[opt.get("quality", "high")]
    crf = opt.get("crf") if opt.get("crf") is not None else q["crf"]
    preset = opt.get("preset") or q["preset"]
    stem = os.path.splitext(out)[0]
    names = [doc.get("players", {}).get("A", "A"), doc.get("players", {}).get("B", "B")]

    info = probe(video, ffprobe)
    if opt.get("size"):
        w, h = (int(x) for x in str(opt["size"]).lower().split("x"))
    elif info:
        w, h = info["w"], info["h"]
    else:
        w, h = BASE_W, BASE_H
        log("⚠ 讀不到影片解析度，計分板以 1920×1080 排版。")

    # ── 影格率：預設跟著片源，桌球快動作不要隨便砍成 30
    fps_opt = opt.get("fps", "source")
    if fps_opt and fps_opt != "source":
        fps_val, fps_arg = float(fps_opt), str(fps_opt)
    elif info and info["fps_frac"]:
        fps_val, fps_arg = info["fps"], info["fps_frac"]
    else:
        fps_val, fps_arg = float(doc.get("fps", 30)), str(doc.get("fps", 30))

    # ── 編碼器與 HDR
    enc = opt.get("encoder") or ("libx264" if q["force_sw"] else HW_ENCODER)
    hdr = is_hdr(info)
    mode = opt.get("hdr", "auto")
    if mode == "auto":
        mode = "keep" if (hdr and "hevc" in enc) else ("tonemap" if hdr else "ignore")
    if mode == "keep" and not hdr:
        log("⚠ 片源不是 HDR，--hdr keep 沒有意義，已忽略。")
        mode = "ignore"
    if mode == "keep" and "hevc" not in enc:
        log("⚠ --hdr keep 需要 HEVC 編碼器，改用 tone-map。")
        mode = "tonemap"
    tonemap = mode == "tonemap" and hdr
    pix_fmt = "p010le" if mode == "keep" else "yuv420p"

    bitrate = opt.get("bitrate") or auto_bitrate(w, h, fps_val, q["scale"])
    sw_bitrate = bool(opt.get("bitrate")) and enc in ("libx264", "libx265")

    hw = opt.get("hwaccel", "auto")
    if hw == "auto":
        hw = "videotoolbox" if IS_MAC else "none"

    # ── 畫質診斷：一眼看出瓶頸在片源還是在轉檔
    if info:
        depth = "10-bit" if is_10bit(info) else "8-bit"
        hdr_tag = f" · {info['trc']} HDR" if hdr else ""
        log(f"片源      {info['w']}×{info['h']} · {info['fps']:.2f} fps · "
            f"{info['codec']} · {depth}{hdr_tag} · {human_bitrate(info['bitrate'])}")
    log(f"輸出      {w}×{h} · {fps_val:.2f} fps · {enc} · {pix_fmt}"
        f"{' · 已 tone-map 成 SDR' if tonemap else ''}")
    log(f"品質      {opt.get('quality', 'high')} · "
        + ("CRF " + str(crf) + f" · preset {preset}"
           if enc in ("libx264", "libx265") and not sw_bitrate
           else "目標碼率 " + bitrate)
        + (f" · 硬體解碼 {hw}" if hw != "none" else ""))
    if info and info["bitrate"]:
        src_mbps = info["bitrate"] / 1e6
        if enc not in ("libx264", "libx265") or sw_bitrate:
            tgt = float(bitrate.rstrip("M"))
            if tgt < src_mbps * 0.8:
                log(f"⚠ 目標碼率低於片源（{tgt:.0f}M < {src_mbps:.0f}M），"
                    f"想保畫質可改 --bitrate {src_mbps * 1.2:.0f}M 或品質選 max")
        if src_mbps < 12 and w * h >= 1920 * 1080:
            log(f"⚠ 片源碼率只有 {src_mbps:.0f} Mbps，畫質上限本來就受限於拍攝端。")

    # ffmpeg 在輸出資料夾裡執行，濾鏡只吃檔名——Windows 的 C:\ 不必跳脫
    workdir = workdir_override or os.path.dirname(os.path.abspath(out)) or "."
    ass_name = os.path.basename(stem) + ".ass"
    flt_name = os.path.basename(stem) + ".filter.txt"

    hold = plan_d.get("hold", 0.0)

    # 強調色：命令列 --accent 優先，其次讀標記 JSON，都沒有就用預設橘
    accent_hex = (opt.get("accent")
                  or (doc.get("scoreboard", {}) or {}).get("accent")
                  or DEFAULT_ACCENT)
    scoreboard_style = scoreboard_style_for(doc, opt)

    with open(os.path.join(workdir, ass_name), "w", encoding="utf-8") as f:
        f.write(build_ass(plan_d["scoring"]["states"], plan_d["src2out"],
                          plan_d["total"], names, w, h,
                          opt.get("font") or FONT_NAME, FONT_NUM,
                          ass_colour(accent_hex),
                          stats=plan_d["stats"] if hold > 0 else None, hold=hold,
                          scoreboard_style=scoreboard_style))
    intro = opt.get("intro") if opt.get("intro") is not None else doc.get("intro")
    intro = intro or {}
    multi = sources and len(sources) > 1
    intro_on = bool(intro.get("enabled"))
    if multi:
        mixed_hdr = any(is_hdr(s) != is_hdr(sources[0]) for s in sources[1:])
        if mixed_hdr:
            log("⚠ 混合 HDR/SDR 片源，逐段轉成 SDR 後接續。")
            mode, tonemap, pix_fmt = "ignore", False, "yuv420p"
        prefix, labels = multi_filter_graph(sources, w, h, fps_arg, intro_on,
                                             mixed_hdr, pix_fmt)
        fgraph = prefix + filter_script(plan_d["keeps"], ass_name, fps_arg,
                                         tonemap, hold, labels[0], labels[1])
    else:
        labels = ("[0:v]", "[0:a]", "[0:v]", "[0:a]")
        fgraph = filter_script(plan_d["keeps"], ass_name, fps_arg, tonemap, hold)
    intro_seconds = 0.0
    if intro.get("enabled"):
        intro_seconds = intro_duration(intro.get("duration", 3.0),
                                       info.get("duration") if info else None)
        intro_name = os.path.basename(stem) + ".intro.ass"
        font = select_font(intro.get("font", ""))
        with open(os.path.join(workdir, intro_name), "w", encoding="utf-8") as f:
            f.write(intro_ass(intro, w, h,
                              intro_seconds, font))
        fgraph = with_intro_filter(fgraph, intro_name, fps_arg, intro_seconds,
                                   font_directory(font), ass_name,
                                   font_directory(opt.get("font") or FONT_NAME),
                                   labels[2], labels[3])
    with open(os.path.join(workdir, flt_name), "w", encoding="utf-8") as f:
        f.write(fgraph)          # 留一份純供除錯查看

    colour_tags = (["-color_primaries", "bt2020", "-color_trc", info["trc"],
                    "-colorspace", "bt2020nc"] if mode == "keep" else
                   ["-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709"])
    tag = ["-tag:v", "hvc1"] if "hevc" in enc else []

    # 只讀到最後一個保留片段為止，不然 ffmpeg 會把整個檔案解碼完
    inputs = ([arg for source in sources for arg in
               (*(["-hwaccel", hw] if hw != "none" else []),
                "-i", os.path.abspath(source["path"]))] if multi else
              [*(["-hwaccel", hw] if hw != "none" else []),
               "-to", f"{plan_d['keeps'][-1][1] + 1:.3f}", "-i", os.path.abspath(video)])
    cmd = [ffmpeg, "-y",
           *(["-progress", "pipe:1", "-nostats"] if progress else []),
           *inputs,
           "-filter_complex", fgraph,
           "-map", "[vout]", "-map", "[aout]" if intro_seconds else "[ac]",
           *video_encoder_args(enc, crf, preset, bitrate, pix_fmt, sw_bitrate),
           *colour_tags, *tag,
           "-c:a", "aac", "-b:a", "256k",
           "-metadata", f"comment=ttcut {VERSION}",
           "-movflags", "+faststart", os.path.abspath(out) if workdir_override else os.path.basename(out)]
    return cmd, workdir, flt_name


def summary_lines(plan_d, opt, video):
    """命令列與介面共用的摘要文字。"""
    fmt, start = plan_d["fmt"], plan_d["start"]
    out = [f"來源      {os.path.basename(video)}",
           f"標記範圍  {ts(plan_d['head'])} → {ts(plan_d['end'])}   {plan_d['span']:.1f}s"]
    rule = ("標準 deuce（勝 2 分）" if fmt["deuce"] == "standard"
            else f"封頂制（10:10 後先到 {fmt['cap']} 分者勝）")
    bits = [f"每局 {fmt['target']} 分", rule]
    if any(start["games"]):
        bits.append(f"起始局數 {start['games'][0]}:{start['games'][1]}")
    if any(start["points"]):
        sc = "每局" if start["scope"] == "every" else "僅第一局"
        bits.append(f"讓分 {start['points'][0]}:{start['points'][1]}（{sc}）")
    out.append(f"賽制      {' · '.join(bits)}")
    out.append(f"事件      {plan_d['points']} 分 · {plan_d['serves']} 發球 · "
               f"{plan_d['serves'] - plan_d['points']} 次重發")
    out.append(f"參數      得分後留 {plan_d['tail']}s · 發球前留 {plan_d['lead']}s · "
               f"最短剪點 {opt.get('min_cut', DEFAULT_MIN_CUT)}s"
               f"{' · 重發也剪' if opt.get('cut_lets') else ''}")
    span, total = plan_d["span"], plan_d["total"]
    if plan_d.get("hold"):
        out.append(f"數據統計  片尾停留 {plan_d['hold']:.1f}s")
    out.append(f"剪去      {len(plan_d['cuts'])} 段 · {span - total:.1f}s")
    out.append(f"成片      {total:.1f}s   壓縮 {(span - total) / span * 100:.0f}%"
               if span > 0 else "成片      0s")
    return out


# ─────────────────────────────────────────── 原生檔案對話框

_MAC_PICK = ('POSIX path of (choose file with prompt "選擇比賽影片"'
             ' of type {"public.movie","public.video"})')

_TK_PICK = (
    "import sys,tkinter,tkinter.filedialog as fd\n"
    "r=tkinter.Tk();r.withdraw();r.attributes('-topmost',True)\n"
    "p=fd.askopenfilename(title='選擇比賽影片',filetypes=["
    "('影片','*.mp4 *.mov *.MOV *.MP4 *.m4v *.avi *.mkv'),('全部','*.*')])\n"
    "sys.stdout.write(p or '')\n")

_MAC_PICK_MULTI = '''set chosen to choose file with prompt "選擇同一場比賽的多段影片" of type {"public.movie","public.video"} with multiple selections allowed
set paths to {}
repeat with itemPath in chosen
    set end of paths to POSIX path of itemPath
end repeat
set AppleScript's text item delimiters to linefeed
return paths as text'''


def native_pick_video():
    """開系統原生檔案對話框，回傳絕對路徑；使用者取消回 None。
    瀏覽器的 <input type=file> 拿不到真實路徑，而 ffmpeg 需要路徑，所以走這裡。"""
    try:
        if IS_MAC:
            r = subprocess.run(["osascript", "-e", _MAC_PICK],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return None                      # 使用者按取消
            p = r.stdout.strip()
        else:
            r = subprocess.run([sys.executable, "-c", _TK_PICK],
                               capture_output=True, text=True, timeout=300)
            p = r.stdout.strip()
        return p if p and os.path.isfile(p) else None
    except Exception:
        return None


def native_pick_videos():
    """Native multi-select; picker order is not reliable, so natural-sort it."""
    try:
        if IS_MAC:
            result = subprocess.run(["osascript", "-e", _MAC_PICK_MULTI],
                                    capture_output=True, text=True, timeout=300)
            paths = result.stdout.splitlines() if result.returncode == 0 else []
        else:
            code = ("import json,tkinter as tk,tkinter.filedialog as fd\n"
                    "r=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
                    "print(json.dumps(fd.askopenfilenames(title='選擇多段比賽影片',"
                    "filetypes=[('影片','*.mp4 *.mov *.MOV *.MP4 *.m4v *.avi *.mkv'),"
                    "('全部','*.*')])))")
            result = subprocess.run([sys.executable, "-c", code],
                                    capture_output=True, text=True, timeout=300)
            paths = json.loads(result.stdout) if result.returncode == 0 else []
        return natural_paths(paths)
    except Exception:
        return None


def native_save_video(default_path):
    """Return a chosen MP4 path, or None when the native dialog is cancelled."""
    try:
        if IS_MAC:
            folder = os.path.dirname(default_path).replace('\\', '\\\\').replace('"', '\\"')
            name = os.path.basename(default_path).replace('\\', '\\\\').replace('"', '\\"')
            script = (f'POSIX path of (choose file name with prompt "另存成片" '
                      f'default name "{name}" default location POSIX file "{folder}/")')
            result = subprocess.run(["osascript", "-e", script],
                                    capture_output=True, text=True, timeout=300)
            path = result.stdout.strip() if result.returncode == 0 else ""
        else:
            code = ("import tkinter as tk,tkinter.filedialog as fd\n"
                    "r=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
                    "print(fd.asksaveasfilename(title='另存成片',defaultextension='.mp4',"
                    "filetypes=[('MP4','*.mp4')],initialdir=" + repr(os.path.dirname(default_path))
                    + ",initialfile=" + repr(os.path.basename(default_path)) + "))")
            result = subprocess.run([sys.executable, "-c", code],
                                    capture_output=True, text=True, timeout=300)
            path = result.stdout.strip() if result.returncode == 0 else ""
        if not path:
            return None
        return path if path.lower().endswith(".mp4") else path + ".mp4"
    except Exception:
        return None


# ─────────────────────────────────────────── 伺服器狀態

STATE = {
    "video": None,          # 目前載入的影片絕對路徑
    "sources": [],          # 同一場比賽依全域時間排列的來源片段
    "custom_out": None,     # 本場比賽手動選擇的另存為路徑
    "ffmpeg": None,
    "ffprobe": "ffprobe",
    "job": None,            # 進行中的渲染
}
STATE_LOCK = threading.Lock()


class Job:
    def __init__(self, out, total):
        self.out = out
        self.total = max(total, 0.001)
        self.pct = 0.0
        self.state = "running"      # running / done / error / cancelled
        self.message = "準備中…"
        self.log = []
        self.proc = None
        self.started = time.time()
        self.speed = ""
        self.thumbnail = None

    def snapshot(self):
        el = time.time() - self.started
        eta = None
        if self.state == "running" and self.pct > 2:
            eta = el * (100 - self.pct) / self.pct
        return dict(state=self.state, pct=round(self.pct, 1),
                    message=self.message, out=self.out, thumbnail=self.thumbnail,
                    elapsed=round(el), eta=round(eta) if eta else None,
                    speed=self.speed, log=self.log[-12:])


_TIME_RE = re.compile(r"out_time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def run_job(job, cmd, workdir, thumbnail_cmd=None):
    """跑 ffmpeg 並解析 -progress 輸出。在背景執行緒中執行。"""
    try:
        job.proc = subprocess.Popen(
            cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as ex:
        job.state, job.message = "error", f"啟動 ffmpeg 失敗：{ex}"
        return

    err_tail = []

    def drain_err():
        for line in job.proc.stderr:
            line = line.rstrip()
            if line:
                err_tail.append(line)
                del err_tail[:-40]
    t = threading.Thread(target=drain_err, daemon=True)
    t.start()

    job.message = "編碼中…"
    for line in job.proc.stdout:
        line = line.strip()
        m = _TIME_RE.search(line)
        if m:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            job.pct = min(99.5, secs / job.total * 100)
        elif line.startswith("speed="):
            job.speed = line.split("=", 1)[1].strip()
        elif line == "progress=end":
            job.pct = 100.0

    job.proc.wait()
    t.join(timeout=2)
    job.proc.stdout.close()
    job.proc.stderr.close()

    if job.state == "cancelled":
        job.message = "已取消"
        return
    if job.proc.returncode == 0:
        if thumbnail_cmd:
            thumb = subprocess.run(thumbnail_cmd, cwd=workdir,
                                   capture_output=True, text=True)
            if thumb.returncode:
                job.state, job.message = "error", "影片完成，但封面產生失敗"
                job.log = thumb.stderr.splitlines()[-12:]
                return
        job.state, job.pct, job.message = "done", 100.0, "完成"
    else:
        job.state = "error"
        job.message = f"ffmpeg 結束碼 {job.proc.returncode}"
        job.log = err_tail[-12:]


def run_job_managed(job, cmd, workdir, thumbnail_cmd, temporary, tags_path, doc):
    try:
        run_job(job, cmd, workdir, thumbnail_cmd)
        if job.state == "done":
            try:
                os.makedirs(os.path.dirname(tags_path), exist_ok=True)
                with open(tags_path, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=2)
            except OSError as ex:
                job.state, job.message = "error", f"成片完成，但標記檔無法儲存：{ex}"
    except Exception as ex:
        job.state, job.message = "error", f"成片流程失敗：{ex}"
    finally:
        temporary.cleanup()


# ─────────────────────────────────────────── 內嵌介面

HTML = r"""<meta charset="utf-8">
<title>ttcut __VERSION__ — 桌球回合標記與剪輯</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --table:#08203A; --table-2:#0F3055; --panel:#0C2A49;
    --line:#3D6B96; --line-soft:#20486E;
    --ink:#E9F2FA; --ink-dim:#8FB2CE;
    --ball:#FF7A18; --warn:#FFC24D; --good:#4ADE80; --bad:#FF6B6B;
    --score:#FF7A18;   /* 計分板強調色，跟著選色器走 */
    --disp:"Avenir Next Condensed","Helvetica Neue Condensed","PingFang TC",system-ui,sans-serif;
    --body:"Helvetica Neue","PingFang TC",system-ui,sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--table);color:var(--ink);
    font-family:var(--body);font-size:14px;-webkit-font-smoothing:antialiased}
  button,input,select{font:inherit;color:inherit}

  .shell{display:grid;grid-template-columns:1fr 336px;grid-template-rows:auto auto 1fr;height:100vh}
  header,.setbar{grid-column:1/-1;display:flex;gap:16px;align-items:center;flex-wrap:wrap;
    padding:9px 16px;background:var(--panel);border-bottom:1px solid var(--line-soft)}
  .setbar{padding:7px 16px;gap:14px;background:var(--table-2)}
  .setbar .grp{display:flex;align-items:center;gap:7px;padding-right:14px;
    border-right:1px solid var(--line-soft)}
  .setbar .grp:last-child{border-right:0}
  .setbar .tag{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-dim)}

  .brand{font-family:var(--disp);font-size:19px;letter-spacing:.12em;text-transform:uppercase;
    display:flex;align-items:center;gap:9px;white-space:nowrap}
  .brand i{width:9px;height:9px;border-radius:50%;background:var(--ball);display:block}
  .brand small{font-family:var(--mono);font-size:10px;letter-spacing:0;color:var(--ink-dim)}
  .ctl{display:flex;align-items:center;gap:6px;color:var(--ink-dim);font-size:12px}
  input[type=text],input[type=number],select{
    background:var(--table);border:1px solid var(--line-soft);border-radius:3px;
    padding:5px 7px;color:var(--ink);font-family:var(--mono);font-size:12px}
  input[type=text]{width:92px;font-family:var(--body)}
  input[type=number]{width:64px}
  #fps{width:78px}   /* 自動偵測會填入 29.97 / 119.88 這類值，需要更寬 */
  select{font-family:var(--body)}
  input:disabled{opacity:.35}
  input[type=color]{width:34px;height:26px;padding:2px;background:var(--table);
    border:1px solid var(--line-soft);border-radius:3px;cursor:pointer}
  .btn{background:transparent;border:1px solid var(--line);border-radius:3px;
    padding:6px 11px;cursor:pointer;font-size:12px;letter-spacing:.04em;transition:background .12s}
  .btn:hover{background:var(--line-soft)}
  .btn:disabled{opacity:.4;cursor:default}
  .btn:disabled:hover{background:transparent}
  .btn:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--ball);outline-offset:1px}
  .btn.hot{border-color:var(--ball);color:var(--ball)}
  .btn.hot:hover:not(:disabled){background:rgba(255,122,24,.14)}
  label.file{position:relative;overflow:hidden}
  label.file input{position:absolute;inset:0;opacity:0;cursor:pointer}
  .srcname{font-family:var(--mono);font-size:11.5px;color:var(--ink-dim);
    max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .sources{display:grid;gap:4px;padding:7px 10px;border:1px solid var(--line-soft);
    border-radius:4px;background:var(--panel);font-size:12px;max-height:170px;overflow-y:auto}
  .sources[hidden]{display:none}
  .source-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .source-row.active{color:var(--ball)}
  .source-row .name{flex:1;min-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .source-row button{padding:2px 6px}
  .source-warning{color:var(--warn);line-height:1.4}

  .stage{display:flex;flex-direction:column;min-width:0;min-height:0;padding:14px 16px;gap:12px}
  .screen{position:relative;flex:1;min-height:0;background:#04121F;border:1px solid var(--line-soft);
    border-radius:4px;display:flex;align-items:center;justify-content:center;overflow:hidden}
  video{max-width:100%;max-height:100%;display:block}
  .empty{color:var(--ink-dim);text-align:center;padding:30px;line-height:1.8;max-width:430px}
  .empty b{color:var(--ink);font-weight:500}
  .roilayer{position:absolute;display:none;border:1px dashed rgba(255,255,255,.42);
    touch-action:none;z-index:2;pointer-events:none}
  .roilayer.selecting{cursor:crosshair;background:rgba(255,122,24,.06);pointer-events:auto}
  .roirect{position:absolute;border:2px solid var(--ball);background:rgba(255,122,24,.11);
    box-shadow:0 0 0 9999px rgba(2,12,22,.28);pointer-events:none}
  .roirect::before{content:'ROI';position:absolute;left:-2px;top:-22px;padding:2px 6px;
    background:var(--ball);color:#04121F;font:10px var(--mono);font-weight:700}

  .transport{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .tc{font-family:var(--mono);font-size:20px;font-variant-numeric:tabular-nums}
  .tc small{font-size:12px;color:var(--ink-dim);margin-left:7px}
  .scrub{flex:1;min-width:160px;accent-color:var(--ball)}
  .speed{display:flex;gap:3px}
  .speed button{padding:4px 8px;font-family:var(--mono);font-size:11px}
  .speed button[aria-pressed=true]{border-color:var(--ball);color:var(--ball)}

  .legend{display:flex;gap:8px;flex-wrap:wrap;font-size:11.5px;color:var(--ink-dim)}
  .legend span{border:1px solid var(--line-soft);border-radius:3px;padding:3px 8px}
  .legend kbd{font-family:var(--mono);color:var(--ink);margin-right:5px}

  .rail{border-left:1px solid var(--line-soft);display:flex;flex-direction:column;
    min-height:0;background:var(--panel)}
  .board{padding:16px 16px 12px;border-bottom:1px solid var(--line-soft)}
  .cards{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .card{position:relative;background:var(--table);border:1px solid var(--line-soft);
    border-radius:4px;padding:8px 0 10px;text-align:center}
  .card.serving{border-color:var(--ball)}
  .card .who{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-dim);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 8px}
  .card .nums{display:flex;align-items:baseline;justify-content:center;gap:12px;margin-top:2px}
  .card .g{font-family:var(--disp);font-size:30px;line-height:1;color:var(--ink-dim)}
  .card .pts{font-family:var(--disp);font-size:58px;line-height:.98;font-variant-numeric:tabular-nums}
  .card.serving .pts{color:var(--score)}
  .card .cap{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-dim);opacity:.7}
  .boardmeta{display:flex;justify-content:space-between;margin-top:10px;font-size:11.5px;color:var(--ink-dim)}
  .boardmeta b{color:var(--ink);font-weight:500}
  .boardmeta .rule{color:var(--warn)}

  .streamhead{display:flex;justify-content:space-between;align-items:center;padding:9px 14px;
    border-bottom:1px solid var(--line-soft);font-size:11px;letter-spacing:.13em;
    text-transform:uppercase;color:var(--ink-dim)}
  .streammeta{display:flex;gap:10px;align-items:center}
  .streammeta .highlights{color:var(--warn)}
  .stream{flex:1;overflow-y:auto;min-height:80px}
  .ev{display:grid;grid-template-columns:60px 1fr auto auto 25px 18px;gap:7px;align-items:center;
    padding:6px 14px;border-bottom:1px solid rgba(32,72,110,.5);cursor:pointer;font-size:12.5px}
  .ev:hover{background:var(--table-2)}
  .ev.highlighted{background:rgba(255,194,77,.055)}
  .ev.highlighted:hover{background:rgba(255,194,77,.095)}
  .ev time{font-family:var(--mono);font-size:11.5px;color:var(--ink-dim)}
  .ev .lbl{display:flex;align-items:center;gap:7px;min-width:0}
  .ev .dot{width:6px;height:6px;border-radius:50%;background:var(--line);flex:none}
  .ev.serve .dot{background:var(--ball)}
  .ev.game .dot{background:var(--warn)}
  .ev .sc{font-family:var(--mono);font-size:11.5px;color:var(--ink-dim)}
  .ev .sc em{color:var(--warn);font-style:normal}
  .ev .highlight-badge{border:1px solid rgba(255,194,77,.55);border-radius:3px;
    padding:1px 5px;color:var(--warn);font-size:10px;white-space:nowrap}
  .ev .star{border:0;background:none;color:var(--ink-dim);cursor:pointer;padding:2px;
    font-size:16px;line-height:1}
  .ev .star:hover,.ev .star[aria-pressed=true]{color:var(--warn)}
  .ev .star-space{width:25px}
  .ev .kill{border:0;background:none;color:var(--ink-dim);cursor:pointer;padding:0 3px;font-size:15px}
  .ev .kill:hover{color:var(--ball)}
  .streamempty{padding:22px 14px;color:var(--ink-dim);font-size:12.5px;line-height:1.7}

  .rallybox{border-bottom:1px solid var(--line-soft);background:rgba(8,32,58,.45)}
  .rallybox summary{padding:9px 14px;cursor:pointer;color:var(--ink-dim);
    font-size:11px;letter-spacing:.12em;text-transform:uppercase}
  .rallybox summary span{float:right;font-family:var(--mono);letter-spacing:0}
  .rallybody{padding:0 14px 10px}
  .rallyctl{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
  .rallyctl button{flex:1;min-width:92px}
  .rallynote{font-size:10.5px;color:var(--ink-dim);line-height:1.45;margin-top:7px}
  .rallydiag{font-family:var(--mono);font-size:9.5px;color:var(--ink-dim);margin-top:5px}
  .rallylist{max-height:205px;overflow-y:auto;margin:8px -14px -10px}
  .rallyrow{display:grid;grid-template-columns:36px 1fr auto;gap:7px;align-items:center;
    padding:7px 14px;border-top:1px solid rgba(32,72,110,.5);cursor:pointer;font-size:11.5px}
  .rallyrow:hover{background:var(--table-2)}
  .rallyrow.low{border-left:3px solid var(--warn);padding-left:11px}
  .rallyrow .rallyseek{border:0;background:none;color:var(--ink);padding:4px 0;
    font:inherit;font-family:var(--mono);text-align:left;cursor:pointer}
  .rallyrow .rallyseek:hover{text-decoration:underline}
  .rallyrow .rallyseek:focus-visible{outline:2px solid var(--ball);outline-offset:2px}
  .rallyrow .rallyinfo{min-width:0}
  .rallyrow small{display:block;color:var(--ink-dim);margin-top:2px;font-family:var(--mono)}
  .rallyrow .confidence{display:inline-block;margin:4px 0 2px;padding:3px 7px;
    border:1px solid var(--warn);border-radius:3px;background:rgba(255,194,77,.14);
    color:var(--warn);font:700 13px var(--body);line-height:1.3}
  .rallyrow [data-rally-serve]{padding:4px 6px}

  .cutout{border-top:1px solid var(--line-soft);padding:11px 14px;font-size:12px;
    color:var(--ink-dim);display:flex;flex-direction:column;gap:5px}
  .cutout .row{display:flex;justify-content:space-between}
  .cutout b{color:var(--ink);font-family:var(--mono);font-weight:400}
  .cutout .save{color:var(--ball)}
  .pads{display:flex;gap:8px 10px;margin-top:4px;flex-wrap:wrap}
  .pads .ctl{font-size:11px}

  .statbox{border-top:1px solid var(--line-soft);padding:11px 14px;
    font-size:12px;color:var(--ink-dim)}   /* 數據統計看板的即時預覽 */
  .statbox .hd{display:flex;justify-content:space-between;margin-bottom:7px;
    font-size:11px;letter-spacing:.13em;text-transform:uppercase}
  .statbox .hd span:last-child{font-family:var(--mono);color:var(--score)}
  .statbox table{width:100%;border-collapse:collapse;font-family:var(--mono);
    font-size:11.5px;table-layout:fixed}
  .statbox th{font-weight:400;color:var(--ink-dim);text-align:right;
    padding:1px 0;font-size:10px;letter-spacing:.06em}
  .statbox td{padding:2px 0;text-align:right;color:var(--ink)}
  .statbox td.nm,.statbox th.nm{text-align:left;font-family:var(--body);
    color:var(--ink-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .statbox td.win{color:var(--score)}
  .statbox td.sub{font-size:9.5px;color:var(--ink-dim)}
  .statbox table.ind{margin-top:8px;border-top:1px solid var(--line-soft);
    padding-top:6px}

  .render{border-top:1px solid var(--line-soft);padding:11px 14px;display:flex;
    flex-direction:column;gap:8px;background:var(--table-2)}
  .render .line{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--ink-dim)}
  .render .out{font-family:var(--mono);font-size:11px;color:var(--ink-dim);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}
  .output-paths{display:grid;gap:4px;font-size:11px;color:var(--ink-dim);overflow-wrap:anywhere}
  .output-paths b{display:inline-block;min-width:38px;color:var(--ink);font-weight:500}
  .go{width:100%;padding:9px;font-size:13px;letter-spacing:.06em}
  .bar{height:5px;background:var(--table);border-radius:3px;overflow:hidden}
  .bar i{display:block;height:100%;width:0;background:var(--ball);
    transition:width .3s linear}
  .bar.done i{background:var(--good)}
  .bar.bad i{background:var(--bad)}
  .pmeta{display:flex;justify-content:space-between;font-size:11px;
    font-family:var(--mono);color:var(--ink-dim)}
  .plog{font-family:var(--mono);font-size:10.5px;color:var(--ink-dim);
    max-height:76px;overflow-y:auto;line-height:1.55;white-space:pre-wrap;word-break:break-all}
  .plog.bad{color:var(--bad)}
  .note{font-size:11px;color:var(--warn);line-height:1.5}
  .intro-fields{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:9px;margin-top:10px}
  .intro-field{display:flex;flex-direction:column;gap:4px;min-width:0;color:var(--ink);font-size:12px}
  .intro-field.full{grid-column:1/-1}
  .intro-field input,.intro-legacy input,#introFont{
    width:100%;min-width:0;background:#071C32;border:1px solid #6085A6;
    border-radius:4px;padding:8px 9px;color:#F7FAFD;font-family:var(--body);font-size:16px;
    -webkit-text-fill-color:#F7FAFD;opacity:1}
  .intro-field input::placeholder,.intro-legacy input::placeholder,#introFont::placeholder{color:#A9BCD0;opacity:1}
  .intro-field input:focus,.intro-legacy input:focus,#introFont:focus{
    border-color:var(--ball);outline:2px solid var(--ball);outline-offset:1px}
  .intro-legacy{display:grid;gap:7px;margin-top:10px}
  .intro-legacy[hidden],.intro-fields[hidden],#introAutofill[hidden]{display:none}
  .intro-legacy p{margin:0;color:var(--ink-dim);font-size:12px;line-height:1.45}
  #introAutofill{margin-top:8px}

  @media (max-width:960px){
    .shell{grid-template-columns:1fr;grid-template-rows:auto auto auto 1fr;height:auto}
    .rail{border-left:0;border-top:1px solid var(--line-soft)}
    .stage{height:54vh}
  }
  @media (max-width:520px){.intro-fields{grid-template-columns:1fr}}
  @media (prefers-reduced-motion:reduce){*{transition:none !important}}
</style>

<div class="shell">
  <header>
    <div class="brand"><i></i>ttcut<small>__VERSION__</small></div>
    <button class="btn" id="pick">載入影片</button>
    <button class="btn" id="pickMulti">載入多段影片…</button>
    <button class="btn" id="newMatch">新增比賽</button>
    <span class="srcname" id="srcname">尚未載入</span>
    <div class="ctl">A<input type="text" id="nameA" value="選手 A"></div>
    <div class="ctl">B<input type="text" id="nameB" value="選手 B"></div>
    <div class="ctl">首發<select id="firstServer"><option value="0">A</option><option value="1">B</option></select></div>
    <span style="flex:1"></span>
    <label class="btn file">讀入標記<input type="file" id="load" accept=".json"></label>
    <button class="btn" id="save">匯出 JSON</button>
    <button class="btn" id="exit">結束 ttcut</button>
  </header>

  <div class="setbar">
    <div class="grp"><span class="tag">影格</span>
      <input type="number" id="fps" value="30" min="1" max="240" step="1"></div>
    <div class="grp"><span class="tag">每局</span>
      <input type="number" id="target" value="11" min="1" step="1"><span class="tag">分</span></div>
    <div class="grp"><span class="tag">賽制</span>
      <select id="deuce">
        <option value="standard">標準 · 勝 2 分</option>
        <option value="capped">封頂 · 先到即勝</option>
      </select>
      <span class="tag">封頂</span><input type="number" id="cap" value="12" min="2" step="1" disabled></div>
    <div class="grp"><span class="tag">起始局數</span>
      <input type="number" id="sgA" value="0" min="0" step="1">
      <span class="tag">:</span>
      <input type="number" id="sgB" value="0" min="0" step="1"></div>
    <div class="grp"><span class="tag">起始分數</span>
      <input type="number" id="spA" value="0" min="0" step="1">
      <span class="tag">:</span>
      <input type="number" id="spB" value="0" min="0" step="1">
      <select id="scope">
        <option value="every">每局套用</option>
        <option value="first">僅第一局</option>
      </select></div>
    <div class="grp"><span class="tag">比分板樣式</span>
      <select id="scoreboardStyle" aria-label="影片比分板樣式">
        <option value="koko" selected>Koko 彩色表格</option>
        <option value="ttcut">ttcut 原版</option>
      </select></div>
    <div class="grp"><span class="tag">原版／統計強調色</span>
      <input type="color" id="accent" value="#FF7A18"
             title="得分數字與名字左側裝飾條共用這個顏色">
      <button class="btn" id="accentReset" title="回到預設橘色">重設</button></div>
  </div>

  <div class="stage">
    <div class="sources" id="sourceList" hidden></div>
    <div class="screen" id="screen">
      <div class="empty" id="empty">
        按左上角 <b>載入影片</b> 選擇比賽影片。<br>
        影片直接從這台電腦讀取，不會上傳到任何地方。
      </div>
      <div class="roilayer" id="roiLayer"><div class="roirect" id="roiRect" hidden></div></div>
    </div>

    <div class="transport">
      <div class="tc"><span id="tc">00:00.00</span><small id="frameno">frame 0</small></div>
      <input type="range" class="scrub" id="scrub" min="0" max="0" step="0.001" value="0" aria-label="播放位置">
      <div class="speed" id="speed">
        <button class="btn" data-r="0.5">.5×</button>
        <button class="btn" data-r="1" aria-pressed="true">1×</button>
        <button class="btn" data-r="1.5">1.5×</button>
        <button class="btn" data-r="2">2×</button>
      </div>
    </div>

    <div class="legend">
      <span><kbd>space</kbd>播放／暫停</span>
      <span><kbd>S</kbd>發球</span>
      <span><kbd>A</kbd>A 得分</span>
      <span><kbd>B</kbd>B 得分</span>
      <span><kbd>H</kbd>精彩球</span>
      <span><kbd>N</kbd>換局</span>
      <span><kbd>Z</kbd>復原</span>
      <span><kbd>← →</kbd>逐格</span>
      <span><kbd>⇧← →</kbd>1 秒</span>
      <span><kbd>⌥← →</kbd>5 秒</span>
      <span><kbd>1-4</kbd>速度</span>
    </div>
  </div>

  <div class="rail">
    <div class="board">
      <div class="cards">
        <div class="card" id="cardA">
          <div class="who" id="whoA">選手 A</div>
          <div class="nums"><span class="g" id="gmA">0</span><span class="pts" id="ptsA">0</span></div>
          <div class="cap">局 · 分</div>
        </div>
        <div class="card" id="cardB">
          <div class="who" id="whoB">選手 B</div>
          <div class="nums"><span class="g" id="gmB">0</span><span class="pts" id="ptsB">0</span></div>
          <div class="cap">局 · 分</div>
        </div>
      </div>
      <div class="boardmeta">
        <span>第 <b id="gameNo">1</b> 局<span id="ruleNote" class="rule"></span></span>
        <span>應由 <b id="expServer">A</b> 發球</span>
      </div>
    </div>

    <details class="rallybox" id="rallyBox">
      <summary>回合候選 · ROI v0.2.3 <span id="rallyCount">—</span></summary>
      <div class="rallybody">
        <div class="rallyctl">
          <button class="btn" id="selectRoi" disabled>框選 ROI</button>
          <button class="btn hot" id="detectRallies" disabled>分析回合</button>
        </div>
        <div class="rallynote" id="rallyNote">先框住這一桌與兩位選手。候選只供預覽；得分仍以 A／B 手動標記為準。</div>
        <div class="rallydiag" id="rallyDiag"></div>
        <div class="rallylist" id="rallyList"></div>
      </div>
    </details>

    <div class="streamhead"><span>事件</span><span class="streammeta"><span class="highlights">精彩球 <b id="highlightCount">0</b></span><span id="evcount">0</span></span></div>
    <div class="stream" id="stream"></div>

    <div class="cutout">
      <div class="row"><span>可剪去區間</span><b id="cutN">0</b></div>
      <div class="row"><span>剪去長度</span><b class="save" id="cutT">0.0s</b></div>
      <div class="row"><span>成片長度</span><b id="outT">0.0s</b></div>
      <div class="pads">
        <div class="ctl">得分後留<input type="number" id="tailPad" value="2.0" step="0.1" min="0">s</div>
        <div class="ctl">發球前留<input type="number" id="leadPad" value="0.8" step="0.1" min="0">s</div>
        <div class="ctl">最短剪點<input type="number" id="minCut" value="2.5" step="0.1" min="0">s</div>
      </div>
    </div>

    <div class="statbox" id="statbox" hidden>
      <div class="hd"><span>數據統計</span><span id="sbResult">0–0</span></div>
      <table id="sbGames"></table>
      <table id="sbInd" class="ind"></table>
    </div>

    <div class="render">
      <div class="line">
        <span>品質</span>
        <select id="quality">
          <option value="fast">快</option>
          <option value="high" selected>標準</option>
          <option value="max">極致（CPU，非常慢）</option>
        </select>
        <label class="ctl" style="margin-left:auto"><input type="checkbox" id="cutLets">重發也剪</label>
      </div>
      <div class="line">
        <label class="ctl"><input type="checkbox" id="stats">數據統計</label>
        <div class="ctl" style="margin-left:auto">片尾停留<input type="number"
             id="statsHold" value="1.0" step="0.5" min="0.2" max="30">s</div>
      </div>
      <div class="line" style="line-height:1.4">標準建議用於一般成片；極致採 CPU 編碼，適合保存版，會慢很多。</div>
      <label class="line">輸出整理方式
        <select id="organization">
          <option value="match-folder" selected>每場比賽建立子資料夾</option>
          <option value="same-folder">同一資料夾</option>
        </select>
      </label>
      <details id="introBox" style="padding:6px 0">
        <summary>片頭與 YouTube 封面</summary>
        <div class="line"><label class="ctl"><input type="checkbox" id="introEnabled">加入片頭</label>
          <label class="ctl">長度 <input type="number" id="introDuration" value="3.0" min="0.1" max="30" step="0.1" style="width:65px"> 秒</label></div>
        <div class="line"><label class="ctl"><input type="checkbox" id="thumbnail">同時輸出 YouTube 封面</label></div>
        <label class="intro-field">片頭字體風格：書法風<input id="introFont" list="introFonts" placeholder="自動選擇；可填已安裝字體"><datalist id="introFonts"></datalist></label>
        <div class="intro-fields" id="introFields">
          <label class="intro-field full">賽事名稱<input id="introTournament" placeholder="例如：北港媽祖盃全國桌球錦標賽"></label>
          <label class="intro-field full">組別<input id="introCategory" placeholder="例如：國小男童一年級以下單打賽"></label>
          <label class="intro-field">選手 A<input id="introPlayerA" placeholder="許宸愷"></label>
          <label class="intro-field">選手 B<input id="introPlayerB" placeholder="曾柏誠"></label>
          <label class="intro-field">學校 A<input id="introSchoolA" placeholder="光復國小"></label>
          <label class="intro-field">學校 B<input id="introSchoolB" placeholder="吉林國小"></label>
        </div>
        <button class="btn" id="introAutofill" type="button">從選手資料帶入</button>
        <div class="intro-legacy" id="introLegacy" hidden>
          <p>舊版片頭保留原本四行排版。若要改成選手與學校對齊，請按「改用新版欄位」。</p>
          <label class="intro-field">舊版第 1 行<input id="intro1"></label>
          <label class="intro-field">舊版第 2 行<input id="intro2"></label>
          <label class="intro-field">舊版第 3 行<input id="intro3"></label>
          <label class="intro-field">舊版第 4 行<input id="intro4"></label>
          <button class="btn" id="introModernize" type="button">改用新版欄位</button>
        </div>
      </details>
      <div class="line"><button class="btn" id="saveAs" disabled>另存為…</button><button class="btn" id="defaultOut" disabled>使用預設位置</button></div>
      <div class="output-paths" id="outputPaths">
        <div><b>MP4</b> <span id="outPath">—</span></div>
        <div><b>封面</b> <span id="thumbPath">—</span></div>
        <div><b>標記</b> <span id="tagsPath">—</span></div>
      </div>
      <button class="btn hot go" id="go" disabled>製作成片</button>
      <div id="progWrap" hidden>
        <div class="bar" id="bar"><i></i></div>
        <div class="pmeta"><span id="pctTxt">0%</span><span id="etaTxt"></span></div>
      </div>
      <div class="plog" id="plog" hidden></div>
      <div class="note" id="note" hidden></div>
    </div>
  </div>
</div>

<script>
(() => {
  const VERSION = "__VERSION__";
  const $ = id => document.getElementById(id);
  const screenEl = $('screen'), emptyEl = $('empty');

  let video = null, events = [], srcName = '', srcPath = '';
  let sources = [], activeSegment = 0, pendingSeek = null, pendingPlay = false;
  let sourceGeometryMismatch = false, separateRois = false, rois = [];
  let outPath = '', customOutput = false, outputSuggestionSeq = 0;
  let ffmpegOK = false, polling = null;
  let rallyCandidates = [], rallyDiagnostics = null, rallyBusy = false;
  let roi = null, roiSelecting = false, roiDrag = null;
  let matchSerial = 0;

  const num = (id, d) => { const v = +$(id).value; return isFinite(v) ? v : d; };
  const fps    = () => Math.max(1, num('fps', 30));
  const target = () => Math.max(1, num('target', 11));
  const deuce  = () => $('deuce').value;
  const capVal = () => Math.max(target(), num('cap', target() + 1));
  const scope  = () => $('scope').value;
  const firstServer = () => +$('firstServer').value;
  const names  = () => [$('nameA').value || 'A', $('nameB').value || 'B'];
  const startGames  = () => [Math.max(0, num('sgA', 0)), Math.max(0, num('sgB', 0))];
  const startPoints = () => [Math.max(0, num('spA', 0)), Math.max(0, num('spB', 0))];

  const fmt = t => {
    if (!isFinite(t) || t < 0) t = 0;
    const m = Math.floor(t / 60), s = t - m * 60;
    return String(m).padStart(2,'0') + ':' + s.toFixed(2).padStart(5,'0');
  };
  const frameOf = t => Math.round(t * fps());
  const mmss = s => {
    if (s == null) return '';
    s = Math.round(s);
    return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');
  };
  const eventPayload = e => ({
    t: +e.t.toFixed(3), frame: frameOf(e.t), type: e.type,
    ...(e.winner === undefined ? {} : {winner: e.winner}),
    ...(e.type === 'point' && e.highlight === true ? {highlight: true} : {})
  });
  const normalizeEvents = raw => (Array.isArray(raw) ? raw : []).map(x => ({
    t: x.t, type: x.type,
    ...(x.winner === undefined ? {} : {winner: x.winner}),
    ...(x.type === 'point' && x.highlight === true ? {highlight: true} : {})
  })).sort((a,b) => a.t - b.t);

  /* ───────────────────────── 送去 Python 的資料
     計分與剪接統計一律由 Python 算，介面不再自己實作一份。 */
  function docPayload() {
    const nm = names(), sg = startGames(), sp = startPoints();
    return {
      version: 2, generator: 'ttcut ' + VERSION, source: srcName, fps: fps(),
      ...(sources.length > 1 ? {sources: sources.map(s => ({path:s.path,
        duration:s.duration, offset:s.offset, end:s.end})), sourceRois: rois,
        separateRois} : {}),
      outputOrganization: $('organization').value,
      pointsPerGame: target(),
      players: {A: nm[0], B: nm[1]},
      firstServer: firstServer() === 0 ? 'A' : 'B',
      format: {pointsPerGame: target(), deuce: deuce(), cap: capVal()},
      start: {games: {A: sg[0], B: sg[1]},
              points: {A: sp[0], B: sp[1]},
              handicapScope: scope()},
      pads: {tail: num('tailPad', 1), lead: num('leadPad', 0.3)},
      scoreboard: {style: $('scoreboardStyle').value, accent: $('accent').value},
      stats: {enabled: $('stats').checked, hold: num('statsHold', 1)},
      intro: introPayload(),
      events: events.map(eventPayload)
    };
  }
  const introIds = ['introTournament','introCategory','introPlayerA','introSchoolA',
                    'introPlayerB','introSchoolB'];
  const introKeys = ['tournament','category','playerA','schoolA','playerB','schoolB'];
  let legacyIntro = false;
  function introMode(legacy) {
    legacyIntro = legacy;
    $('introFields').hidden = legacy;
    $('introAutofill').hidden = legacy;
    $('introLegacy').hidden = !legacy;
  }
  function loadIntro(data) {
    introIds.forEach(id => $(id).value = '');
    [1,2,3,4].forEach(i => $('intro'+i).value = '');
    const hasFields = introKeys.some(k => Object.prototype.hasOwnProperty.call(data, k));
    if (hasFields) {
      introKeys.forEach((k,i) => $(introIds[i]).value = data[k] || '');
      introMode(false);
      return;
    }
    const lines = (data.lines || []).slice(0,4);
    if (lines.length) {
      const matchup = (lines[2] || '').match(/^\s*(.+?)\s+VS\s+(.+?)\s*$/i);
      const schools = (lines[3] || '').match(/^\s*(.+?)\s{2,}(.+?)\s*$/);
      if (matchup && schools) {
        [lines[0],lines[1],matchup[1],schools[1],matchup[2],schools[2]]
          .forEach((value,i) => $(introIds[i]).value = (value || '').trim());
        introMode(false);
      } else {
        lines.forEach((line,i) => $('intro'+(i+1)).value = line);
        introMode(true);
      }
    } else introMode(false);
  }
  const introPayload = () => {
    const base = {enabled: $('introEnabled').checked,
      duration: num('introDuration', 3), thumbnail: $('thumbnail').checked,
      font: $('introFont').value.trim()};
    if (legacyIntro) return {...base, lines: [1,2,3,4].map(i => $('intro'+i).value.trim())};
    introKeys.forEach((k,i) => base[k] = $(introIds[i]).value.trim());
    return base;
  };
  const safeFilenamePart = value => String(value || '')
    .replace(/[\/\\:*?"<>|]|\p{Cc}|\p{Cf}/gu, '_')
    .replace(/_+/g, '_').replace(/^[\s._]+|[\s._]+$/g, '');
  function introFilename(intro) {
    const parts = introKeys.map(key => safeFilenamePart(intro[key]));
    if (parts.some(part => !/[\p{L}\p{N}]/u.test(part))) return null;
    const [event, category, playerA, schoolA, playerB, schoolB] = parts;
    return `${event}_${category}_${playerA}(${schoolA})VS${playerB}(${schoolB})`
      .replace(/_+/g, '_').replace(/^[\s._]+|[\s._]+$/g, '') + '.mp4';
  }
  try {
    const savedMode = localStorage.getItem('ttcut.outputOrganization');
    if (['match-folder','same-folder'].includes(savedMode)) $('organization').value = savedMode;
  } catch (e) { /* storage may be disabled */ }
  function showOutputPlan(layout) {
    $('outPath').textContent = layout.out;
    $('thumbPath').textContent = $('introEnabled').checked && $('thumbnail').checked
      ? layout.thumbnail : '—（未勾選）';
    $('tagsPath').textContent = layout.tags;
  }
  function localOutputPlan(base) {
    const slash = Math.max(base.lastIndexOf('/'), base.lastIndexOf('\\'));
    const root = base.slice(0,slash+1), filename = base.slice(slash+1);
    const stem = filename.replace(/\.mp4$/i,'');
    const folder = $('organization').value === 'match-folder' ? root+stem+'/' : root;
    return {out:folder+filename, thumbnail:folder+stem+'.thumbnail.jpg',
      tags:folder+($('organization').value === 'match-folder' ? 'ttcut-data/' : '')+
        stem+'.tags.json'};
  }
  function updateOutputSuggestion() {
    const seq = ++outputSuggestionSeq;
    if (!srcPath) return;
    if (!customOutput) {
      const name = introFilename(introPayload()) ||
        srcPath.split(/[\\/]/).pop().replace(/\.[^.]+$/, '') + '.cut.mp4';
      const slash = Math.max(srcPath.lastIndexOf('/'), srcPath.lastIndexOf('\\'));
      outPath = srcPath.slice(0, slash + 1) + name;
    }
    showOutputPlan(localOutputPlan(outPath));
    fetch('/suggest-output', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({intro:introPayload(), organization:$('organization').value,
        customOutput})})
      .then(r => r.json()).then(d => {
        if (seq !== outputSuggestionSeq || !d.layout) return;
        showOutputPlan(d.layout);
      }).catch(() => {});
  }
  $('organization').addEventListener('change', () => {
    try { localStorage.setItem('ttcut.outputOrganization', $('organization').value); }
    catch (e) { /* storage may be disabled */ }
    updateOutputSuggestion();
  });
  ['introEnabled','thumbnail'].forEach(id => $(id).addEventListener('change', updateOutputSuggestion));
  introIds.concat(['intro1','intro2','intro3','intro4']).forEach(id =>
    $(id).addEventListener('input', updateOutputSuggestion));
  $('introModernize').addEventListener('click', () => {
    if (!confirm('舊版對戰與學校無法可靠拆分。改用新版欄位後，請重新填寫兩位選手與學校；確定繼續？')) return;
    $('introTournament').value = $('intro1').value;
    $('introCategory').value = $('intro2').value;
    introMode(false);
    updateOutputSuggestion();
  });
  $('introAutofill').addEventListener('click', () => {
    for (const side of ['A','B']) {
      const label = $('name'+side).value.trim();
      const m = label.match(/^(.+?)\s*[（(]([^()（）]+)[)）]\s*$/);
      if (m) {
        $('introPlayer'+side).value = m[1].trim();
        $('introSchool'+side).value = m[2].trim();
      } else if (label && label !== '選手 '+side && !/[()（）]/.test(label)) {
        $('introPlayer'+side).value = label;
      }
    }
    updateOutputSuggestion();
  });
  const optPayload = () => ({
    min_cut: num('minCut', 2), cut_lets: $('cutLets').checked,
    quality: $('quality').value,
    stats: $('stats').checked, stats_hold: num('statsHold', 1),
    intro: introPayload()
  });

  function clearMatch() {
    matchSerial++; seq++;
    if (video) { video.pause(); video.remove(); video = null; }
    srcPath = ''; srcName = ''; outPath = ''; customOutput = false;
    sources = []; activeSegment = 0; pendingSeek = null; pendingPlay = false;
    sourceGeometryMismatch = false; separateRois = false; rois = [];
    outputSuggestionSeq++; events = [];
    roi = null; roiSelecting = false; roiDrag = null;
    rallyCandidates = []; rallyDiagnostics = null; rallyBusy = false;
    $('nameA').value = '選手 A'; $('nameB').value = '選手 B';
    $('firstServer').value = '0';
    $('target').value = '11'; $('deuce').value = 'standard'; $('cap').value = '12';
    ['sgA','sgB','spA','spB'].forEach(id => $(id).value = '0');
    loadIntro({});
    $('scope').value = 'every'; $('fps').value = '30';
    $('srcname').textContent = '尚未載入'; $('srcname').title = '';
    $('outPath').textContent = '—'; $('scrub').value = '0'; $('scrub').max = '0';
    $('thumbPath').textContent = '—'; $('tagsPath').textContent = '—';
    $('sourceList').hidden = true; $('sourceList').innerHTML = '';
    $('tc').textContent = '00:00.00'; $('frameno').textContent = 'frame 0';
    $('progWrap').hidden = true; $('plog').hidden = true; $('statbox').hidden = true;
    emptyEl.style.display = ''; $('roiLayer').style.display = 'none';
    $('rallyBox').open = false; paintRoi(); paintRallies();
    $('saveAs').disabled = true; $('defaultOut').disabled = true;
    banner(null); refresh(); updateGo(false);
  }

  $('newMatch').addEventListener('click', async () => {
    if (polling) { banner('請等成片完成再新增比賽。'); return; }
    if (events.length && !confirm('目前標記尚未匯出 JSON。確定新增比賽並清除標記？')) return;
    const r = await fetch('/new-match', {method:'POST'});
    const d = await r.json();
    if (!r.ok) { banner(d.error); return; }
    clearMatch();
  });

  $('saveAs').addEventListener('click', async () => {
    const r = await fetch('/save-as', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({intro:introPayload()})});
    const d = await r.json();
    if (!r.ok) return banner(d.error);
    if (d.path) {
      customOutput = true; outputSuggestionSeq++;
      outPath = d.path; updateOutputSuggestion();
    }
  });
  $('defaultOut').addEventListener('click', async () => {
    if (!srcPath) return;
    const r = await fetch('/default-output', {method:'POST'});
    if (!r.ok) return banner((await r.json()).error);
    customOutput = false; updateOutputSuggestion();
  });
  $('exit').addEventListener('click', async () => {
    if (polling) { banner('請等成片完成再結束。'); return; }
    const r = await fetch('/exit', {method:'POST'});
    const d = await r.json();
    if (!r.ok) return banner(d.error);
    document.body.innerHTML = '<main style="padding:40px;font:20px system-ui">ttcut 已結束，可以關閉此頁面。</main>';
    window.close();
  });

  /* ───────────────────────── 影片 */
  const totalDuration = () => sources.length ? sources[sources.length - 1].end :
    (video ? video.duration || 0 : 0);
  const sourceForTime = value => {
    const t = Math.max(0, Math.min(totalDuration(), value));
    const index = sources.findIndex((s,i) => t < s.end || i === sources.length - 1);
    return {index: Math.max(0, index), local: sources.length ?
      Math.max(0, Math.min(sources[Math.max(0,index)].duration,
                            t - sources[Math.max(0,index)].offset)) : t};
  };
  const escapeText = value => String(value).replace(/[&<>"']/g,
    ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const requireSeparateRois = () => sourceGeometryMismatch || separateRois;
  function paintSourceList() {
    const box = $('sourceList');
    box.hidden = sources.length < 2;
    if (box.hidden) return;
    box.innerHTML = `<strong>目前順序（初次載入依自然檔名排序）· 總長 ${fmt(totalDuration())}</strong>` +
      `<label><input type="checkbox" id="separateRoisToggle" ${requireSeparateRois() ? 'checked' : ''}
        ${sourceGeometryMismatch ? 'disabled' : ''}> 鏡位不同時逐段框選 ROI</label>` +
      sources.map((s,i) => `<div class="source-row${i === activeSegment ? ' active' : ''}">
        <button type="button" class="btn" data-source-seek="${i}">${i+1}</button>
        <span class="name" title="${escapeText(s.path)}">${escapeText(s.path.split(/[\\/]/).pop())}</span>
        <span>${fmt(s.duration)} · ${fmt(s.offset)} → ${fmt(s.end)}</span>
        <button type="button" class="btn" data-source-up="${i}" ${i ? '' : 'disabled'}>↑</button>
        <button type="button" class="btn" data-source-down="${i}" ${i+1 < sources.length ? '' : 'disabled'}>↓</button>
      </div>`).join('') +
      (sourceGeometryMismatch ? '<div class="source-warning">影片解析度不同：回合分析前請逐段框選 ROI。</div>' :
        '<div class="source-warning">若鏡位或球桌位置改變，請勾選逐段 ROI。</div>') +
      (sources.some(s => s.codec !== sources[0].codec || s.fps_frac !== sources[0].fps_frac ||
                         s.w !== sources[0].w || s.h !== sources[0].h ||
                         JSON.stringify(s.audio) !== JSON.stringify(sources[0].audio))
        ? '<div class="source-warning">片源規格不同，成片時會逐段調整後接續。</div>' : '');
  }
  function setSources(data) {
    sources = data.sources || [];
    sourceGeometryMismatch = !!data.geometryMismatch;
    separateRois = sourceGeometryMismatch;
    rois = Array(sources.length).fill(null);
    activeSegment = 0; pendingSeek = null; pendingPlay = false;
    roi = null; rallyCandidates = []; rallyDiagnostics = null;
    paintSourceList(); paintRoi(); paintRallies();
    if (data.sourceWarning) banner(data.sourceWarning);
  }
  $('sourceList').addEventListener('click', async e => {
    const seek = e.target.closest('[data-source-seek]');
    if (seek) { seekGlobal(sources[+seek.dataset.sourceSeek].offset); return; }
    const up = e.target.closest('[data-source-up]');
    const down = e.target.closest('[data-source-down]');
    if (!up && !down) return;
    if (events.length) { banner('已有標記時不能調整片段順序；請先新增比賽再選好順序。'); return; }
    const index = +(up ? up.dataset.sourceUp : down.dataset.sourceDown);
    const other = index + (up ? -1 : 1);
    const order = sources.map((_,i) => i);
    [order[index],order[other]] = [order[other],order[index]];
    const r = await fetch('/reorder-sources', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({order})});
    const d = await r.json();
    if (!r.ok) return banner(d.error);
    setSources(d); srcPath = sources[0].path;
    srcName = srcPath.split(/[\\/]/).pop(); $('srcname').textContent = srcName;
    mountVideo(); updateOutputSuggestion(); refresh();
  });
  $('sourceList').addEventListener('change', e => {
    if (e.target.id !== 'separateRoisToggle') return;
    separateRois = e.target.checked;
    if (separateRois) rois[activeSegment] = roi;
    else roi = rois[activeSegment] || roi;
    rallyCandidates = []; rallyDiagnostics = null; paintRallies(); updateGo();
  });
  async function pickVideos(multi) {
    if (polling) { banner('請等成片完成再更換影片。'); return; }
    if (events.length && !confirm('切換影片會清除目前標記。確定繼續？')) return;
    const button = $(multi ? 'pickMulti' : 'pick');
    button.disabled = true;
    try {
      const r = await fetch(multi ? '/pick-multi' : '/pick-video', {method: 'POST'});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '載入影片失敗');
      if (d.cancelled || !d.path) return;
      clearMatch();
      srcPath = d.path; srcName = d.name; outPath = d.defaultOut;
      setSources(d);
      $('srcname').textContent = d.name;
      $('srcname').title = d.path;
      $('outPath').textContent = d.defaultOut;
      $('saveAs').disabled = false; $('defaultOut').disabled = false;
      updateOutputSuggestion();
      if (d.info) {
        if (d.info.fps) $('fps').value = Math.round(d.info.fps * 100) / 100;
        $('srcname').title = `${d.path}\n${d.info.w}×${d.info.h} · ${d.info.fps}fps · ${d.info.codec}`;
      }
      mountVideo();
      updateGo();
      refresh();
    } catch (e) {
      banner('讀取影片失敗：' + e.message);
    } finally { button.disabled = false; }
  }
  $('pick').addEventListener('click', () => pickVideos(false));
  $('pickMulti').addEventListener('click', () => pickVideos(true));

  function mountVideo() {
    if (video) { video.pause(); video.remove(); }
    video = document.createElement('video');
    activeSegment = 0;
    video.src = '/video?segment=0&t=' + Date.now();
    video.preload = 'auto'; video.playsInline = true;
    emptyEl.style.display = 'none';
    screenEl.appendChild(video);
    video.addEventListener('loadedmetadata', () => {
      $('scrub').max = totalDuration();
      if (pendingSeek !== null) {
        video.currentTime = Math.min(video.duration || pendingSeek, pendingSeek);
        pendingSeek = null;
      }
      if (pendingPlay) { pendingPlay = false; video.play().catch(() => {}); }
      updateRoiLayer(); tick(); updateGo();
    });
    video.addEventListener('timeupdate', tick);
    video.addEventListener('seeked', tick);
    video.addEventListener('ended', () => {
      if (sources.length > 1 && activeSegment + 1 < sources.length)
        seekGlobal(sources[activeSegment + 1].offset, true);
    });
    video.addEventListener('error', () => banner('影片無法播放，可能是瀏覽器不支援這個編碼。'));
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(updateRoiLayer).observe(video);
  }

  function updateRoiLayer() {
    const layer = $('roiLayer');
    if (!video || !video.clientWidth || !video.clientHeight) { layer.style.display = 'none'; return; }
    layer.style.display = 'block';
    layer.style.left = video.offsetLeft + 'px'; layer.style.top = video.offsetTop + 'px';
    layer.style.width = video.clientWidth + 'px'; layer.style.height = video.clientHeight + 'px';
    paintRoi();
  }

  function paintRoi() {
    const rect = $('roiRect');
    rect.hidden = !roi;
    if (!roi) return;
    rect.style.left = (roi.x * 100) + '%'; rect.style.top = (roi.y * 100) + '%';
    rect.style.width = (roi.w * 100) + '%'; rect.style.height = (roi.h * 100) + '%';
  }

  function setRoiSelecting(on) {
    roiSelecting = on;
    $('roiLayer').classList.toggle('selecting', on);
    $('selectRoi').textContent = on ? '拖曳框選…' : (roi ? '重選 ROI' : '框選 ROI');
    $('rallyNote').textContent = on
      ? '請在影片上拖曳，框住本桌與兩位選手的主要活動範圍。'
      : (roi ? 'ROI 已設定。可開始分析；候選不會自動改動標記。'
             : '先框住這一桌與兩位選手。候選只供預覽；得分仍以 A／B 手動標記為準。');
    updateGo();
  }

  $('selectRoi').addEventListener('click', () => {
    if (!video) return;
    video.pause(); setRoiSelecting(!roiSelecting);
  });
  $('roiLayer').addEventListener('pointerdown', e => {
    if (!roiSelecting) return;
    const b = $('roiLayer').getBoundingClientRect();
    roiDrag = {x: Math.max(0, Math.min(b.width, e.clientX - b.left)),
               y: Math.max(0, Math.min(b.height, e.clientY - b.top)), b};
    $('roiLayer').setPointerCapture(e.pointerId);
  });
  $('roiLayer').addEventListener('pointermove', e => {
    if (!roiDrag) return;
    const x = Math.max(0, Math.min(roiDrag.b.width, e.clientX - roiDrag.b.left));
    const y = Math.max(0, Math.min(roiDrag.b.height, e.clientY - roiDrag.b.top));
    const x0 = Math.min(roiDrag.x, x), y0 = Math.min(roiDrag.y, y);
    roi = {x: x0 / roiDrag.b.width, y: y0 / roiDrag.b.height,
           w: Math.abs(x - roiDrag.x) / roiDrag.b.width,
           h: Math.abs(y - roiDrag.y) / roiDrag.b.height};
    paintRoi();
  });
  $('roiLayer').addEventListener('pointerup', e => {
    if (!roiDrag) return;
    $('roiLayer').releasePointerCapture(e.pointerId); roiDrag = null;
    if (!roi || roi.w < .08 || roi.h < .08) {
      roi = null; paintRoi();
      $('rallyNote').textContent = '框選範圍太小，請包含球桌與兩位選手。';
      updateGo(); return;
    }
    roi = Object.fromEntries(Object.entries(roi).map(([k,v]) => [k, +v.toFixed(6)]));
    if (requireSeparateRois()) rois[activeSegment] = roi;
    rallyCandidates = []; rallyDiagnostics = null; paintRallies();
    setRoiSelecting(false);
  });

  function now() {
    if (!video) return 0;
    // Paused seeks may not produce a video-frame callback. The media element
    // time is authoritative for the clock, scrubber, and new manual marks.
    return (sources[activeSegment]?.offset || 0) +
      (pendingSeek !== null ? pendingSeek : video.currentTime);
  }
  function seekGlobal(t, play=false) {
    if (!video) return;
    const target = sourceForTime(t);
    if (target.index === activeSegment) {
      if (pendingSeek !== null) {
        pendingSeek = target.local;
        pendingPlay = pendingPlay || play;
      } else {
        video.currentTime = target.local;
        if (play) video.play().catch(() => {});
      }
    } else {
      video.pause();
      activeSegment = target.index;
      roi = requireSeparateRois() ? rois[activeSegment] : roi;
      paintRoi(); paintSourceList(); updateRoiLayer();
      pendingSeek = target.local; pendingPlay = play;
      video.src = '/video?segment=' + activeSegment + '&t=' + Date.now();
      video.load();
    }
    tick();
  }
  function tick() {
    if (!video) return;
    const t = now();
    $('tc').textContent = fmt(t);
    $('frameno').textContent = 'frame ' + frameOf(t);
    if (document.activeElement !== $('scrub')) $('scrub').value = t;
  }
  $('scrub').addEventListener('input', e => seekGlobal(+e.target.value));

  function seekBy(sec) {
    if (!video) return;
    video.pause();
    const t = Math.max(0, Math.min(totalDuration(), now() + sec));
    seekGlobal(Math.round(t * fps()) / fps());
  }
  function setRate(r) {
    if (video) video.playbackRate = r;
    [...$('speed').children].forEach(b => b.setAttribute('aria-pressed', String(+b.dataset.r === r)));
  }
  $('speed').addEventListener('click', e => {
    const b = e.target.closest('button'); if (b) setRate(+b.dataset.r);
  });

  /* ───────────────────────── 事件 */
  function add(type, winner) {
    if (!video) return;
    const t = Math.round(now() * fps()) / fps();
    events.push(winner === undefined ? {t, type} : {t, type, winner});
    events.sort((a,b) => a.t - b.t);
    refresh();
  }
  function undo() {
    if (!events.length) return;
    let idx = 0;
    for (let i = 1; i < events.length; i++) if (events[i].t >= events[idx].t) idx = i;
    events.splice(idx, 1); refresh();
  }

  function completedPointIndexes(list=events) {
    const completed = new Set();
    let hasServe = false;
    list.forEach((event, index) => {
      if (event.type === 'serve') hasServe = true;
      else if (event.type === 'point') {
        if (hasServe) completed.add(index);
        hasServe = false;
      } else if (event.type === 'game') hasServe = false;
    });
    return {completed, open:hasServe};
  }

  function toggleHighlightAt(index) {
    const {completed} = completedPointIndexes();
    const event = events[index];
    if (!event || !completed.has(index)) {
      banner('只能標記已有發球與得分的完成回合。');
      return false;
    }
    event.highlight = event.highlight !== true;
    if (!event.highlight) delete event.highlight;
    refresh();
    return true;
  }

  function toggleLatestHighlight() {
    const {completed, open} = completedPointIndexes();
    if (open) {
      banner('請先按 A 或 B 完成這一回合，再標記精彩球。');
      return false;
    }
    const indexes = [...completed];
    if (!indexes.length) {
      banner('尚無可標記的完成回合。');
      return false;
    }
    return toggleHighlightAt(indexes[indexes.length - 1]);
  }

  /* ───────────────────────── Rally Detection v0.2.3
     ROI 影像才會產生候選；音訊只佐證接近門檻的視覺片段。
     只有使用者按下「確認發球」才會寫入事件，得分者仍完全手動。 */
  function paintRallies() {
    $('rallyCount').textContent = rallyCandidates.length || '—';
    const d = rallyDiagnostics;
    $('rallyDiag').textContent = d && d.motionThreshold != null
      ? `ROI ${roi ? [roi.x, roi.y, roi.w, roi.h].map(v => v.toFixed(3)).join(',') : '—'} · ` +
        `motion 基線 ${d.motionBaseline} · 閾值 ${d.motionThreshold}` +
        `${d.motionSupportThreshold != null ? '/' + d.motionSupportThreshold + '（佐證）' : ''} · ` +
        `${d.frames} frames · audio ${d.audio && d.audio.available ? d.audio.impacts + ' hits（輔助）' : '無'} · ` +
        `佐證保留 ${d.audioPromotedCandidates || 0} · 前後修剪 ${d.audioTrimmedCandidates || 0} · ` +
        `短走動排除 ${d.rejectedBriefCandidates || 0} · valley 切分 ${d.motionValleySplits || 0}`
      : '';
    if (!rallyCandidates.length) { $('rallyList').innerHTML = ''; return; }
    const splitReasons = {
      short_candidate: '候選太短', no_sustained_valley: '沒有持續低動作',
      brief_valley: '低動作太短',
      edge_valley: '低動作在邊緣', short_side: '切後一側太短',
      shallow_valley: '動作下降不足', no_visual_restart: '後段未重新活動',
      weak_visual_before: '前段活動不足',
      low_split_confidence: '切分信心不足',
      fragment_guard: '避免切成碎片',
      split_limit: '單候選最多兩個切點',
      sustained_motion_valley_visual_restart: '持續低動作後重新活動'
    };
    const shortReasons = {
      few_strong_frames: '強動作影格少', weak_visual_peak: '視覺峰值弱',
      no_audio_support: '無輔助音訊', very_short: '時間很短',
      incomplete_rise_fall: '起落動作不完整'
    };
    $('rallyList').innerHTML = rallyCandidates.map((r, i) => {
      const used = events.some(e => e.type === 'serve' && Math.abs(e.t - r.start) < .20);
      const valley = r.motionValleyScore == null ? '—' :
        `${r.motionValleyScore}/${r.motionValleyDuration}s`;
      const split = `切點 ${r.splitPoint == null ? '—' : fmt(r.splitPoint)} · ` +
        `valley ${valley} · 間隔 audio ${r.splitAudioHits || 0} · ` +
        `切分信心 ${r.splitConfidence == null ? '—' : Math.round(r.splitConfidence * 100) + '%'} ` +
        `(視覺 ${r.splitVisualConfidence == null ? '—' : Math.round(r.splitVisualConfidence * 100) + '%'}, ` +
        `音訊扣 ${Math.round((r.splitAudioPenalty || 0) * 100)}%) · ` +
        `${r.splitDecision === 'split' ? '已切' : '未切'}：${splitReasons[r.splitReason] || r.splitReason || '—'}`;
      const checks = (r.splitChecks || []).filter(v => v.point != null);
      const se = r.shortEvidence || {};
      const shortInfo = se.penalty > 0
        ? `<small>短候選：基礎 ${Math.round(r.baseConfidence * 100)}% → ` +
          `${Math.round(r.confidence * 100)}% · 起／落 ${se.rise}/${se.fall} · ` +
          `${(se.penaltyReasons || []).map(v => shortReasons[v] || v).join('、')}</small>`
        : '';
      const allValleys = checks.length ? `<details><summary>檢查 ${checks.length} 個 valley</summary>` +
        checks.map(v => `<small>${fmt(v.point)} · ${v.motionValleyScore}/${v.motionValleyDuration}s` +
          ` · 深度 ${v.valleyDepth == null ? '—' : Math.round(v.valleyDepth * 100) + '%'}` +
          ` · audio ${v.audioHits}（扣 ${Math.round((v.audioPenalty || 0) * 100)}%）` +
          ` · 切分 ${v.splitConfidence == null ? '—' : Math.round(v.splitConfidence * 100) + '%'}` +
          ` · ${v.decision === 'split' ? '已切' : '未切'}：${splitReasons[v.reason] || v.reason}</small>`).join('') +
        `</details>` : '';
      return `<div class="rallyrow${r.confidenceTier === 'low' ? ' low' : ''}" data-rally="${i}" title="點一下從候選開頭預覽">
        <button type="button" class="rallyseek" data-rally-seek="${i}" aria-label="預覽候選 ${i + 1}，從 ${fmt(r.start)} 開始">#${String(i + 1).padStart(2, '0')}</button>
        <div class="rallyinfo">${fmt(r.start)} → ${fmt(r.end)} · ${r.duration}s
          ${r.confidenceTier === 'low' ? `<strong class="confidence">低信心 · ${Math.round(r.confidence * 100)}% · 請人工確認</strong>` : ''}
          <small>motion ${r.motionMean}/${r.motionPeak} · 左右 ${r.sideBalance} · ` +
          `frames ${r.strongFrames || 0}/${r.supportFrames || 0} · audio ${r.audioHits} · ` +
          `${r.boundaryBasis || 'motion'} · score ${Math.round(r.confidence * 100)}%</small>` +
          `<small>${split}</small>${shortInfo}${allValleys}</div>
        <button type="button" class="btn" data-rally-serve="${i}" ${used ? 'disabled' : ''}>${used ? '已加入' : '確認發球'}</button>
      </div>`;
    }).join('');
  }

  $('detectRallies').addEventListener('click', async () => {
    if (!srcPath || rallyBusy ||
        (requireSeparateRois() ? rois.some(value => !value) : !roi)) return;
    const serial = matchSerial;
    rallyBusy = true; updateGo(); $('rallyBox').open = true;
    $('rallyNote').textContent = '正在分析 ROI 影像；音訊只作輔助，長影片需要稍等一下…';
    try {
      const r = await fetch('/detect-rallies', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({roi, rois, separateRois:requireSeparateRois(),
          duration: totalDuration()})
      });
      const d = await r.json();
      if (serial !== matchSerial) return;
      if (!r.ok) throw new Error(d.error || '分析失敗');
      rallyCandidates = d.candidates || []; rallyDiagnostics = d.diagnostics || null;
      paintRallies();
      $('rallyNote').textContent = rallyCandidates.length
        ? `找到 ${rallyCandidates.length} 個視覺候選。點區間預覽；只有按「確認發球」才會加入事件。`
        : '這個 ROI 沒有找到足夠明確的視覺候選；請重選更貼近本桌與兩位選手的範圍。';
    } catch (e) {
      if (serial !== matchSerial) return;
      $('rallyNote').textContent = '回合分析失敗：' + e.message + '。原本的手動標記功能不受影響。';
    } finally {
      if (serial === matchSerial) { rallyBusy = false; updateGo(); }
    }
  });

  $('rallyList').addEventListener('click', e => {
    const use = e.target.closest('[data-rally-serve]');
    const row = e.target.closest('[data-rally]');
    if (!row || !video) return;
    const candidate = rallyCandidates[+row.dataset.rally];
    if (!candidate) return;
    if (use) {
      const t = Math.round(candidate.start * fps()) / fps();
      events.push({t, type: 'serve'}); events.sort((a,b) => a.t - b.t);
      paintRallies(); refresh();
      return;
    }
    // The whole candidate list lives inside #rallyBox (<details>). Only the
    // nested diagnostic disclosure control should suppress preview seeking.
    if (e.target.closest('.rallyrow summary')) return;
    video.pause(); seekGlobal(candidate.start); tick();
  });

  /* ───────────────────────── 向 Python 要計分結果 */
  let seq = 0, timer = null;
  function refresh() {
    paintRallies();
    clearTimeout(timer);
    timer = setTimeout(doRefresh, 50);
  }
  async function doRefresh() {
    const mine = ++seq;
    $('cap').disabled = deuce() !== 'capped';
    try {
      const r = await fetch('/fold', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({doc: docPayload(), opt: optPayload()})
      });
      const st = await r.json();
      if (mine !== seq) return;               // 過期回應直接丟掉
      paint(st);
      banner(null);
    } catch (e) {
      banner('連不到本機服務，請確認終端機視窗還開著。');
    }
  }

  /* ───────────────────────── 畫面 */
  function paint(st) {
    const nm = names(), cur = st.cur;
    $('whoA').textContent = nm[0]; $('whoB').textContent = nm[1];
    $('ptsA').textContent = cur.a;  $('ptsB').textContent = cur.b;
    $('gmA').textContent = cur.gA;  $('gmB').textContent = cur.gB;
    $('gameNo').textContent = cur.gameNo;
    $('ruleNote').textContent = deuce() === 'capped' ? ` · 封頂 ${capVal()}` : '';
    $('expServer').textContent = nm[cur.server];
    $('cardA').classList.toggle('serving', cur.server === 0);
    $('cardB').classList.toggle('serving', cur.server === 1);

    const stream = $('stream');
    $('evcount').textContent = events.length;
    $('highlightCount').textContent = events.filter(e =>
      e.type === 'point' && e.highlight === true).length;
    if (!events.length) {
      stream.innerHTML = '<div class="streamempty">還沒有事件。<br>播放影片，在發球觸拍的瞬間按 <b style="color:var(--ink)">S</b>，得分時按 <b style="color:var(--ink)">A</b> 或 <b style="color:var(--ink)">B</b>。</div>';
    } else {
      let prevServe = false, html = '';
      const {completed} = completedPointIndexes();
      events.forEach((e, i) => {
        const s = st.snaps[i];
        let label, cls;
        if (e.type === 'serve') { cls = 'serve'; label = prevServe ? '發球 · 重發' : '發球'; }
        else if (e.type === 'game') { cls = 'game'; label = '換局'; }
        else { cls = 'point'; label = nm[e.winner === 'A' ? 0 : 1] + ' 得分'; }
        prevServe = e.type === 'serve';
        const sc = s ? (s.won ? `<em>${s.gA}–${s.gB} 局</em>` : `${s.a}–${s.b}`) : '';
        const highlighted = e.type === 'point' && e.highlight === true;
        const badge = highlighted ? '<span class="highlight-badge">★ 精彩球</span>' : '';
        const star = completed.has(i)
          ? `<button class="star" data-highlight="${i}" aria-pressed="${highlighted}"
              title="${highlighted ? '取消精彩球' : '標記精彩球'}" aria-label="${highlighted ? '取消精彩球' : '標記精彩球'}">★</button>`
          : '<span class="star-space"></span>';
        html += `<div class="ev ${cls}${highlighted ? ' highlighted' : ''}" data-i="${i}">
          <time>${fmt(e.t)}</time>
          <span class="lbl"><i class="dot"></i>${label}${badge}</span>
          <span class="sc">${s && s.won ? `${s.a}–${s.b}` : ''}</span>
          <span class="sc">${sc}</span>
          ${star}
          <button class="kill" data-kill="${i}" title="刪除">×</button>
        </div>`;
      });
      stream.innerHTML = html;
      stream.scrollTop = stream.scrollHeight;
    }

    const c = st.cuts || {};
    $('cutN').textContent = c.n || 0;
    $('cutT').textContent = (c.seconds || 0).toFixed(1) + 's';
    $('outT').textContent = (c.outSeconds || 0).toFixed(1) + 's'
      + (c.pct ? `　壓縮 ${c.pct}%` : '');
    paintStats(st);
    updateGo(st.ok);
  }

  /* ───────────────────────── 數據統計看板：先在介面上看到數字，不必等渲染完 */
  const esc = s => String(s).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

  function paintStats(st) {
    const on = $('stats').checked, s = st.stats;
    $('statbox').hidden = !on || !s;
    if (!on || !s) return;
    const nm = names(), L = {"games": "局數", "l1": "總得分數", "l2": "發球得分率", "l3": "最多連續", "dash": "–", "none": "—"};
    const win = (c, v) => `<td class="${c ? 'win' : ''}">${v}</td>`;

    $('sbResult').textContent = s.gamesWon[0] + '\u2013' + s.gamesWon[1];

    let h = '<tr><th class="nm"></th><th>' + L.games + '</th>' +
            s.games.map(g => '<th>G' + g.no + '</th>').join('') + '</tr>';
    [0, 1].forEach(i => {
      h += '<tr><td class="nm">' + esc(nm[i]) + '</td>' +
           win(s.gamesWon[i] > s.gamesWon[1 - i], s.gamesWon[i]) +
           s.games.map(g => win(g.winner === i,
                                (i ? g.b : g.a) === null ? L.dash : (i ? g.b : g.a))).join('') +
           '</tr>';
    });
    $('sbGames').innerHTML = h;

    const pct = i => s.serveRate[i] === null ? L.none
                     : Math.round(s.serveRate[i] * 100) + '%';
    const rows = [
      [L.l1, String(s.points[0]), String(s.points[1]), '', ''],
      [L.l2, pct(0), pct(1),
       s.served[0] ? s.servedWon[0] + '/' + s.served[0] : '',
       s.served[1] ? s.servedWon[1] + '/' + s.served[1] : ''],
      [L.l3, String(s.streak[0]), String(s.streak[1]), '', '']
    ];
    const num = v => v === L.none ? -1 : parseFloat(v);
    $('sbInd').innerHTML =
      '<tr><th class="nm"></th><th>' + esc(nm[0]) + '</th><th>' + esc(nm[1]) + '</th></tr>' +
      rows.map(r => {
        const a = num(r[1]), b = num(r[2]);
        return '<tr><td class="nm">' + r[0] + '</td>' +
          win(a > b, r[1] + (r[3] ? ' <span class="sub">' + r[3] + '</span>' : '')) +
          win(b > a, r[2] + (r[4] ? ' <span class="sub">' + r[4] + '</span>' : '')) +
          '</tr>';
      }).join('');
  }

  function updateGo(planOK) {
    const running = polling !== null;
    const ok = !!srcPath && ffmpegOK && events.length > 0 && planOK !== false;
    $('go').disabled = running || !ok;
    if (!ffmpegOK && srcPath) $('go').textContent = '找不到 ffmpeg';
    else $('go').textContent = running ? '製作中…' : '製作成片';
    $('selectRoi').disabled = !video || rallyBusy;
    $('detectRallies').disabled = rallyBusy || !srcPath || !ffmpegOK ||
      (requireSeparateRois() ? rois.length !== sources.length ||
        rois.some(value => !value) : !roi) || roiSelecting;
    $('detectRallies').textContent = rallyBusy ? '分析中…' : '分析回合';
  }

  function banner(msg) {
    const n = $('note');
    if (!msg) { n.hidden = true; return; }
    n.hidden = false; n.textContent = msg;
  }

  $('stream').addEventListener('click', e => {
    const h = e.target.closest('[data-highlight]');
    if (h) { toggleHighlightAt(+h.dataset.highlight); return; }
    const k = e.target.closest('[data-kill]');
    if (k) { events.splice(+k.dataset.kill, 1); refresh(); return; }
    const row = e.target.closest('.ev');
    if (row && video) { video.pause(); seekGlobal(events[+row.dataset.i].t); tick(); }
  });

  function paintAccent() {
    document.documentElement.style.setProperty('--score', $('accent').value);
  }
  $('accent').addEventListener('input', paintAccent);
  $('accentReset').addEventListener('click', () => {
    $('accent').value = '#FF7A18'; paintAccent();
  });

  ['nameA','nameB','firstServer','target','fps','tailPad','leadPad','minCut',
   'deuce','cap','sgA','sgB','spA','spB','scope','cutLets','stats','statsHold']
    .forEach(id => $(id).addEventListener('input', refresh));

  /* ───────────────────────── 鍵盤 */
  function isEditableTarget(el) {
    if (!el) return false;
    if (el.isContentEditable || /^(INPUT|SELECT|TEXTAREA|BUTTON|SUMMARY)$/.test(el.tagName || ''))
      return true;
    return !!(el.closest && el.closest('[contenteditable]:not([contenteditable="false"])'));
  }
  function handleKeydown(e) {
    const el = e.target || document.activeElement;
    if (isEditableTarget(el)) return;
    if (e.metaKey || e.ctrlKey) return;
    const step = e.altKey ? 5 : e.shiftKey ? 1 : 1 / fps();
    switch (e.key) {
      case ' ':          if (video) video.paused ? video.play() : video.pause(); break;
      case 'ArrowLeft':  seekBy(-step); break;
      case 'ArrowRight': seekBy(step); break;
      case 's': case 'S': add('serve'); break;
      case 'a': case 'A': add('point', 'A'); break;
      case 'b': case 'B': add('point', 'B'); break;
      case 'h': case 'H': toggleLatestHighlight(); break;
      case 'n': case 'N': add('game'); break;
      case 'z': case 'Z': undo(); break;
      case '1': setRate(0.5); break;
      case '2': setRate(1); break;
      case '3': setRate(1.5); break;
      case '4': setRate(2); break;
      default: return;
    }
    e.preventDefault();
  }
  addEventListener('keydown', handleKeydown);

  /* ───────────────────────── 渲染 */
  $('go').addEventListener('click', async () => {
    $('go').disabled = true;
    $('plog').hidden = true; $('plog').classList.remove('bad');
    $('bar').classList.remove('done','bad');
    $('progWrap').hidden = false;
    setBar(0, '啟動 ffmpeg…');
    try {
      const r = await fetch('/render', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({doc: docPayload(), opt: optPayload(),
          out: outPath, customOutput, organization:$('organization').value})
      });
      const d = await r.json();
      if (d.error) { fail(d.error); return; }
      if (d.layout) showOutputPlan(d.layout);
      if (d.notes && d.notes.length) {
        $('plog').hidden = false; $('plog').textContent = d.notes.join('\n');
      }
      startPolling();
    } catch (e) { fail(e.message); }
  });

  function setBar(pct, right) {
    $('bar').querySelector('i').style.width = Math.max(0, Math.min(100, pct)) + '%';
    $('pctTxt').textContent = pct.toFixed(0) + '%';
    $('etaTxt').textContent = right || '';
  }
  function fail(msg) {
    $('bar').classList.add('bad');
    $('plog').hidden = false; $('plog').classList.add('bad');
    $('plog').textContent = msg;
    polling = null; updateGo();
  }

  function startPolling() {
    polling = setInterval(async () => {
      try {
        const s = await (await fetch('/render/status')).json();
        if (s.state === 'running') {
          setBar(s.pct, (s.eta != null ? '剩約 ' + mmss(s.eta) : '') +
                        (s.speed ? '　' + s.speed : ''));
          updateGo();
        } else {
          clearInterval(polling); polling = null;
          if (s.state === 'done') {
            setBar(100, '耗時 ' + mmss(s.elapsed));
            $('bar').classList.add('done');
            $('plog').hidden = false;
            $('plog').textContent = '完成 → ' + s.out +
              (s.thumbnail ? '\nYouTube 封面 → ' + s.thumbnail : '');
            if (!customOutput) updateOutputSuggestion();
          } else if (s.state === 'cancelled') {
            setBar(s.pct, '已取消');
          } else {
            fail((s.message || '渲染失敗') + '\n' + (s.log || []).join('\n'));
          }
          updateGo();
        }
      } catch (e) {
        clearInterval(polling); polling = null;
        fail('失去與本機服務的連線。');
      }
    }, 400);
    updateGo();
  }

  /* ───────────────────────── 存讀 */
  $('save').addEventListener('click', async () => {
    const doc = docPayload();
    try {                       // 讓匯出的 JSON 也帶上剪點，跟 V1.22 格式一致
      const st = await (await fetch('/fold', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({doc, opt: optPayload()})
      })).json();
      if (st.cutList) doc.cuts = st.cutList;
    } catch (e) { /* 拿不到就不帶，不影響主要資料 */ }
    const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)], {type:'application/json'}));
    const a = document.createElement('a');
    a.href = url;
    a.download = (srcName.replace(/\.[^.]+$/, '') || 'match') + '.tags.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  $('load').addEventListener('change', e => {
    const f = e.target.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = async () => {
      try {
        const d = JSON.parse(r.result);
        if (d.sources && d.sources.length) {
          const loaded = await fetch('/load-sources', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({sources:d.sources})});
          const sourceData = await loaded.json();
          if (!loaded.ok) throw new Error(sourceData.error || '無法讀入來源影片。');
          clearMatch();
          srcPath = sourceData.path; srcName = sourceData.name;
          outPath = sourceData.defaultOut; setSources(sourceData);
          if (Array.isArray(d.sourceRois) && d.sourceRois.length === sources.length)
            rois = d.sourceRois;
          separateRois = sourceGeometryMismatch || !!d.separateRois;
          roi = requireSeparateRois() ? rois[0] : (rois[0] || null);
          paintSourceList(); paintRoi();
          $('srcname').textContent = srcName; $('srcname').title = srcPath;
          $('saveAs').disabled = false; $('defaultOut').disabled = false;
          mountVideo(); updateOutputSuggestion();
        }
        events = normalizeEvents(d.events);
        if (d.fps) $('fps').value = d.fps;
        if (d.players) { $('nameA').value = d.players.A; $('nameB').value = d.players.B; }
        if (d.firstServer) $('firstServer').value = d.firstServer === 'B' ? '1' : '0';
        if (d.pads) { $('tailPad').value = d.pads.tail; $('leadPad').value = d.pads.lead; }

        const F = d.format || {};
        $('target').value = F.pointsPerGame || d.pointsPerGame || 11;
        $('deuce').value  = F.deuce || 'standard';
        $('cap').value    = F.cap || (+$('target').value + 1);
        const S = d.start || {}, g = S.games || {}, p = S.points || {};
        $('sgA').value = g.A || 0; $('sgB').value = g.B || 0;
        $('spA').value = p.A || 0; $('spB').value = p.B || 0;
        $('scope').value = S.handicapScope || 'every';
        $('scoreboardStyle').value = (d.scoreboard || {}).style || 'ttcut';
        $('accent').value = (d.scoreboard || {}).accent || '#FF7A18';
        const SB = d.stats || {};
        $('stats').checked = !!SB.enabled;
        if (SB.hold) $('statsHold').value = SB.hold;
        if (d.intro) {
          $('introEnabled').checked = !!d.intro.enabled;
          $('introDuration').value = d.intro.duration || 3;
          $('thumbnail').checked = !!d.intro.thumbnail;
          $('introFont').value = d.intro.font || '';
          loadIntro(d.intro);
          updateOutputSuggestion();
        }
        if (['match-folder','same-folder'].includes(d.outputOrganization)) {
          $('organization').value = d.outputOrganization;
          try { localStorage.setItem('ttcut.outputOrganization', d.outputOrganization); }
          catch (e) { /* storage may be disabled */ }
          updateOutputSuggestion();
        }
        paintAccent();
        refresh();
      } catch (err) {
        banner(err.message || '讀不到這個檔案的標記資料，請確認是本工具匯出的 JSON。');
      }
    };
    r.readAsText(f);
    e.target.value = '';
  });

  /* ───────────────────────── 起始：接回已載入的狀態（重新整理也不會掉） */
  (async () => {
    try {
      const s = await (await fetch('/state')).json();
      ffmpegOK = !!s.ffmpeg;
      $('introFonts').innerHTML = (s.introFonts || []).map(f =>
        '<option value="' + f.replace(/&/g,'&amp;').replace(/"/g,'&quot;') + '"></option>').join('');
      if (s.video) {
        srcPath = s.video; srcName = s.videoName;
        setSources(s);
        customOutput = !!s.customOut;
        outPath = s.customOut || srcPath.replace(/\.[^.]+$/, '') + '.cut.mp4';
        $('outPath').textContent = outPath;
        $('saveAs').disabled = false; $('defaultOut').disabled = false;
        $('srcname').textContent = s.videoName;
        $('srcname').title = s.video;
        mountVideo();
        updateOutputSuggestion();
      }
      if (!s.ffmpeg) banner('找不到 ffmpeg，可以標記與匯出 JSON，但無法產出成片。');
      if (s.job && s.job.state === 'running') { $('progWrap').hidden = false; startPolling(); }
    } catch (e) { /* 服務還沒起來就算了 */ }
    refresh();
  })();
})();
</script>
"""

# ─────────────────────────────────────────── HTTP 伺服器

def guess_type(path):
    t, _ = mimetypes.guess_type(path)
    if not t:
        ext = os.path.splitext(path)[1].lower()
        t = {".mov": "video/quicktime", ".mp4": "video/mp4",
             ".m4v": "video/x-m4v", ".mkv": "video/x-matroska"}.get(ext, "video/mp4")
    return t


PREVIEW_DISCONNECT_ERRNOS = {
    errno.EPIPE,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ENOBUFS,
}


def is_preview_disconnect(ex):
    """只辨識瀏覽器放棄預覽 Range request 時會出現的 socket 錯誤。"""
    return (isinstance(ex, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError))
            or getattr(ex, "errno", None) in PREVIEW_DISCONNECT_ERRNOS)


class Handler(BaseHTTPRequestHandler):
    server_version = f"ttcut/{VERSION}"

    def log_message(self, *a):
        pass                                    # 別把每個 range 請求都印出來洗版

    # ── 小工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _write(self, data):
        try:
            self.wfile.write(data)
        except OSError as ex:
            if not is_preview_disconnect(ex):
                raise
            # 瀏覽器中斷 range 請求是常態；停止這次傳送，不重試。

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ── 路由
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._html()
        if p == "/video":
            return self._video()
        if p == "/state":
            with STATE_LOCK:
                v = STATE["video"]
                sources = list(STATE["sources"])
                job = STATE["job"]
                custom_out = STATE["custom_out"]
            return self._json(dict(
                video=v, videoName=os.path.basename(v) if v else None,
                **self._source_payload(sources),
                customOut=custom_out,
                ffmpeg=STATE["ffmpeg"],
                job=job.snapshot() if job else None,
                introFonts=[f for f in FONT_PREFERENCES if f in installed_families()]))
        if p == "/render/status":
            with STATE_LOCK:
                job = STATE["job"]
            return self._json(job.snapshot() if job else dict(state="idle"))
        self.send_error(404)

    def _same_origin(self):
        """擋掉別的網頁對這個本機服務發請求（例如偷偷叫出檔案對話框）。
        同源的 fetch 一定會帶 Origin，所以「有帶但對不上」就拒絕。"""
        o = self.headers.get("Origin")
        if o is None:
            return True
        host = self.headers.get("Host", "")
        return o in (f"http://{host}", f"https://{host}")

    def do_POST(self):
        p = urlparse(self.path).path
        if not self._same_origin():
            return self._json(dict(error="cross-origin request rejected"), 403)
        try:
            if p == "/fold":
                return self._fold()
            if p == "/detect-rallies":
                return self._detect_rallies()
            if p == "/pick-video":
                return self._pick()
            if p == "/pick-multi":
                return self._pick_multi()
            if p == "/load-sources":
                return self._load_sources()
            if p == "/reorder-sources":
                return self._reorder_sources()
            if p == "/save-as":
                return self._save_as()
            if p == "/default-output":
                return self._default_output()
            if p == "/suggest-output":
                return self._suggest_output()
            if p == "/new-match":
                return self._new_match()
            if p == "/exit":
                return self._exit()
            if p == "/render":
                return self._render()
            if p == "/render/cancel":
                return self._cancel()
        except Exception as ex:
            return self._json(dict(error=f"{type(ex).__name__}: {ex}"), 500)
        self.send_error(404)

    # ── 首頁
    def _html(self):
        body = HTML.replace("__VERSION__", VERSION).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    # ── 影片串流（必須支援 Range，否則滑桿拖不動、Safari 可能不播）
    def _video(self):
        with STATE_LOCK:
            path = STATE["video"]
            sources = STATE["sources"]
        try:
            index = int((urlparse(self.path).query.split("segment=", 1)[1]).split("&", 1)[0])
            if index < 0 or index >= len(sources):
                return self.send_error(404, "no such segment")
            path = sources[index]["path"]
        except (IndexError, ValueError):
            pass
        if not path or not os.path.isfile(path):
            return self.send_error(404, "no video loaded")
        size = os.path.getsize(path)
        ctype = guess_type(path)
        rng = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:                          # bytes=-N 取尾端 N 位元組
                    start = max(0, size - int(e))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError as ex:
            if not is_preview_disconnect(ex):
                raise
            # seek／跨檔時瀏覽器已改送新的 Range request，舊串流直接結束。

    # ── 計分（唯一權威）
    def _fold(self):
        req = self._body()
        doc = req.get("doc") or {}
        opt = req.get("opt") or {}
        pl = plan(doc, opt)
        sc = pl["scoring"]
        res = dict(cur=sc["cur"], snaps=sc["snaps"], ok=pl["ok"], reason=pl["reason"],
                   stats=pl["stats"], statsOn=pl["stats_on"], hold=pl["hold"])
        if pl["ok"]:
            span, total = pl["span"], pl["total"]
            res["cuts"] = dict(
                n=len(pl["cuts"]),
                seconds=round(span - total, 1),
                dropped=len(pl["dropped"]),
                outSeconds=round(total, 1),
                pct=round((span - total) / span * 100) if span > 0 else 0)
            res["cutList"] = [{"from": round(f, 3), "to": round(t, 3)}
                              for f, t, _ in pl["cuts"]]
        else:
            res["cuts"] = dict(n=0, seconds=0.0, dropped=0, outSeconds=0.0, pct=0)
        return self._json(res)

    # ── 實驗性 ROI 回合候選（不寫入事件、不判斷勝方）
    def _detect_rallies(self):
        with STATE_LOCK:
            video = STATE["video"]
            sources = list(STATE["sources"])
            ffmpeg = STATE["ffmpeg"]
        if not video:
            return self._json(dict(error="還沒有載入影片。"), 400)
        if not ffmpeg:
            return self._json(dict(error="找不到 ffmpeg，無法分析影片。"), 400)
        if detect_video is None:
            return self._json(dict(error="找不到 rally_detection.py。"), 503)
        req = self._body()
        roi = req.get("roi")
        duration = req.get("duration")
        try:
            duration = float(duration) if duration is not None else None
            if len(sources) < 2:
                return self._json(detect_video(video, roi, ffmpeg, duration=duration))
            rois = req.get("rois") or []
            separate = geometry_mismatch(sources) or bool(req.get("separateRois"))
            if separate and (len(rois) != len(sources) or any(not value for value in rois)):
                return self._json(dict(error="請逐段框選 ROI 後再分析。"), 400)
            candidates, diagnostics = [], []
            for i, source in enumerate(sources):
                local_roi = rois[i] if separate else roi
                result = detect_video(source["path"], local_roi, ffmpeg,
                                      duration=source["duration"])
                offset = source["offset"]
                for item in result.get("candidates", []):
                    item = dict(item)
                    for key in ("start", "end", "visualStart", "visualEnd", "splitPoint"):
                        if item.get(key) is not None:
                            item[key] = round(item[key] + offset, 3)
                    item["splitChecks"] = [dict(check, point=round(check["point"] + offset, 3))
                                           if check.get("point") is not None else check
                                           for check in item.get("splitChecks", [])]
                    item["segment"] = i
                    candidates.append(item)
                diagnostics.append(dict(segment=i, source=os.path.basename(source["path"]),
                                        offset=offset, **result.get("diagnostics", {})))
            return self._json(dict(candidates=sorted(candidates, key=lambda x: x["start"]),
                                   diagnostics=dict(segments=diagnostics)))
        except (DetectionError, TypeError, ValueError) as ex:
            return self._json(dict(error=str(ex)), 400)

    @staticmethod
    def _source_payload(sources):
        issues = compatibility_issues(sources)
        return dict(sources=sources,
                    totalDuration=sources[-1]["end"] if sources else 0.0,
                    geometryMismatch=geometry_mismatch(sources) if sources else False,
                    sourceWarning=("片源的" + "、".join(issues) +
                                   "不同；成片時會在同一次轉檔中逐段調整。" if issues else ""))

    def _set_sources(self, paths):
        sources = make_sources(paths, probe, probe_audio, STATE["ffprobe"])
        with STATE_LOCK:
            STATE["sources"] = sources
            STATE["video"] = sources[0]["path"]
            STATE["custom_out"] = None
        return self._json(dict(path=sources[0]["path"],
                               name=os.path.basename(sources[0]["path"]),
                               info=dict(w=sources[0]["w"], h=sources[0]["h"],
                                         fps=round(sources[0]["fps"], 2),
                                         codec=sources[0]["codec"],
                                         duration=sources[0]["duration"]),
                               defaultOut=default_out(sources[0]["path"]),
                               **self._source_payload(sources)))

    # ── 原生檔案對話框
    def _pick(self):
        with STATE_LOCK:
            if STATE["job"] and STATE["job"].state == "running":
                return self._json(dict(error="請等成片完成，再開啟另一支影片。"), 409)
        p = native_pick_video()
        if not p:
            return self._json(dict(cancelled=True))
        try:
            return self._set_sources([p])
        except SourceError as ex:
            return self._json(dict(error=str(ex)), 400)

    def _pick_multi(self):
        with STATE_LOCK:
            if STATE["job"] and STATE["job"].state == "running":
                return self._json(dict(error="請等成片完成，再開啟另一場比賽。"), 409)
        paths = native_pick_videos()
        if not paths:
            return self._json(dict(cancelled=True))
        if len(paths) < 2:
            return self._json(dict(error="請至少選擇兩段影片。"), 400)
        try:
            return self._set_sources(paths)
        except SourceError as ex:
            return self._json(dict(error=str(ex)), 400)

    def _load_sources(self):
        with STATE_LOCK:
            if STATE["job"] and STATE["job"].state == "running":
                return self._json(dict(error="請等成片完成再讀入標記。"), 409)
        raw = self._body().get("sources") or []
        if not raw:
            return self._json(dict(error="JSON 沒有影片來源。"), 400)
        try:
            return self._set_sources([item.get("path", "") for item in raw])
        except SourceError as ex:
            return self._json(dict(error=str(ex)), 400)

    def _reorder_sources(self):
        order = self._body().get("order")
        with STATE_LOCK:
            if STATE["job"] and STATE["job"].state == "running":
                return self._json(dict(error="請等成片完成再調整順序。"), 409)
            current = list(STATE["sources"])
        try:
            sources = reorder_sources(current, order)
        except (SourceError, TypeError) as ex:
            return self._json(dict(error=str(ex)), 400)
        with STATE_LOCK:
            STATE["sources"] = sources
            STATE["video"] = sources[0]["path"]
        return self._json(self._source_payload(sources))

    def _save_as(self):
        with STATE_LOCK:
            video = STATE["video"]
        if not video:
            return self._json(dict(error="請先載入影片。"), 400)
        intro = (self._body().get("intro") or {})
        path = native_save_video(default_out(video, intro))
        if path:
            with STATE_LOCK:
                STATE["custom_out"] = path
        return self._json(dict(path=path, cancelled=not bool(path)))

    def _default_output(self):
        with STATE_LOCK:
            STATE["custom_out"] = None
        return self._json(dict(ok=True))

    def _suggest_output(self):
        with STATE_LOCK:
            video = STATE["video"]
            custom_out = STATE["custom_out"]
        if not video:
            return self._json(dict(error="請先載入影片。"), 400)
        req = self._body()
        intro = req.get("intro") or {}
        mode = req.get("organization", "same-folder")
        use_custom = bool(req.get("customOutput") and custom_out)
        base = custom_out if use_custom else default_out(video, intro)
        try:
            layout = output_layout(base, mode, automatic=not use_custom)
        except ValueError as ex:
            return self._json(dict(error=str(ex)), 400)
        return self._json(dict(path=layout["out"], layout=layout))

    def _new_match(self):
        with STATE_LOCK:
            if STATE["job"] and STATE["job"].state == "running":
                return self._json(dict(error="請等成片完成，再新增比賽。"), 409)
            STATE["video"] = None
            STATE["sources"] = []
            STATE["job"] = None
            STATE["custom_out"] = None
        return self._json(dict(ok=True))

    def _exit(self):
        if self.client_address[0] != "127.0.0.1":
            return self._json(dict(error="local only"), 403)
        with STATE_LOCK:
            if STATE["job"] and STATE["job"].state == "running":
                return self._json(dict(error="請等成片完成再結束，或回到終端機按 Ctrl-C。"), 409)
        self._json(dict(ok=True))
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # ── 渲染
    def _render(self):
        with STATE_LOCK:
            job = STATE["job"]
            video = STATE["video"]
            sources = list(STATE["sources"])
            ffmpeg = STATE["ffmpeg"]
        if job and job.state == "running":
            return self._json(dict(error="已經有一個渲染在進行中。"), 409)
        if not video:
            return self._json(dict(error="還沒有載入影片。"), 400)
        if not ffmpeg:
            return self._json(dict(error="找不到 ffmpeg，無法渲染。"), 400)

        req = self._body()
        doc = req.get("doc") or {}
        opt = req.get("opt") or {}
        naming_intro = opt.get("intro") if opt.get("intro") is not None else doc.get("intro")
        custom_output = bool(req.get("customOutput"))
        organization = req.get("organization", "same-folder")
        base = req.get("out") if custom_output else default_out(video, naming_intro)
        if custom_output and not base:
            return self._json(dict(error="請先選擇輸出位置。"), 400)
        try:
            layout = output_layout(base, organization, automatic=not custom_output)
        except ValueError as ex:
            return self._json(dict(error=str(ex)), 400)
        if custom_output and organization == "match-folder" and layout["conflict"]:
            return self._json(dict(error="同名比賽資料夾已存在，請在另存為中選新檔名。"), 409)
        out = layout["out"]
        if any(os.path.realpath(out) == os.path.realpath(s["path"])
               for s in (sources or [dict(path=video)])):
            return self._json(dict(error="不能覆蓋原始影片。"), 400)
        if not os.path.isdir(os.path.dirname(base)):
            return self._json(dict(error="輸出資料夾不存在。"), 400)

        pl = plan(doc, opt)
        if not pl["ok"]:
            return self._json(dict(error=pl["reason"]), 400)
        if sources and pl["keeps"][-1][1] > sources[-1]["end"] + .05:
            return self._json(dict(error="標記時間超出來源影片總長度。"), 400)
        if len(sources) > 1:
            doc = dict(doc, sources=[{k: s[k] for k in ("path", "duration", "offset", "end")}
                                     for s in sources])
        doc["outputOrganization"] = organization

        logs = []
        issues = compatibility_issues(sources)
        if issues:
            logs.append("⚠ 片源" + "、".join(issues) + "不同；逐段調整後接續。")
        intro = opt.get("intro") if opt.get("intro") is not None else doc.get("intro")
        intro = intro or {}
        if intro.get("enabled") and not intro_has_text(intro):
            return self._json(dict(error="請先填寫至少一行片頭文字。"), 400)
        os.makedirs(layout["folder"], exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="ttcut-")
        try:
            cmd, workdir, _ = build_render(doc, pl, video, out, opt, ffmpeg,
                                           STATE["ffprobe"], log=logs.append,
                                           progress=True, sources=sources,
                                           workdir_override=temporary.name)
        except Exception:
            temporary.cleanup()
            raise
        intro_seconds = (intro_duration(intro.get("duration", 3.0),
                         (probe(video, STATE["ffprobe"]) or {}).get("duration"))
                         if intro.get("enabled") else 0.0)
        thumbnail_cmd = None
        if intro_seconds and intro.get("thumbnail"):
            thumbnail_cmd = thumbnail_command(ffmpeg, video, out,
                             os.path.basename(os.path.splitext(out)[0]) + ".intro.ass",
                             font_directory(select_font(intro.get("font", ""))))
            thumbnail_cmd[-1] = layout["thumbnail"]
        new = Job(out, pl["total"] + pl.get("hold", 0.0) + intro_seconds)
        new.thumbnail = layout["thumbnail"] if thumbnail_cmd else None
        new.log = logs
        with STATE_LOCK:
            STATE["job"] = new
        threading.Thread(target=run_job_managed,
                         args=(new, cmd, workdir, thumbnail_cmd, temporary,
                               layout["tags"], doc),
                         daemon=True).start()
        return self._json(dict(ok=True, out=out, layout=layout,
                               thumbnail=layout["thumbnail"] if thumbnail_cmd else None,
                               total=round(pl["total"] + intro_seconds, 1),
                               summary=summary_lines(pl, opt, video), notes=logs))

    def _cancel(self):
        with STATE_LOCK:
            job = STATE["job"]
        if job and job.state == "running" and job.proc:
            job.state = "cancelled"
            try:
                job.proc.terminate()
            except Exception:
                pass
        return self._json(dict(ok=True))


def safe_filename_part(value):
    """Keep Chinese and normal punctuation; replace only unsafe filename characters."""
    value = ''.join('_' if ch in '/\\:*?"<>|' or
                    unicodedata.category(ch) in ('Cc', 'Cf') else ch
                    for ch in str(value or ''))
    return re.sub(r'^[\s._]+|[\s._]+$', '', re.sub(r'_+', '_', value))


def intro_filename(intro):
    """Return a complete structured title filename, or None for partial/legacy data."""
    keys = ("tournament", "category", "playerA", "schoolA", "playerB", "schoolB")
    parts = [safe_filename_part(intro.get(key)) for key in keys]
    if not all(re.search(r'[^\W_]', part, re.UNICODE) for part in parts):
        return None
    tournament, category, player_a, school_a, player_b, school_b = parts
    stem = f"{tournament}_{category}_{player_a}({school_a})VS{player_b}({school_b})"
    return re.sub(r'^[\s._]+|[\s._]+$', '', re.sub(r'_+', '_', stem)) + ".mp4"


def default_out(video, intro=None):
    intro = intro or {}
    name = intro_filename(intro)
    if name is None:
        name = os.path.splitext(os.path.basename(video))[0] + ".cut.mp4"
    return os.path.join(os.path.dirname(os.path.abspath(video)), name)


def unique_default_out(video, intro=None):
    path = default_out(video, intro)
    stem, ext = os.path.splitext(path)
    number = 2
    while os.path.exists(path):
        path = f"{stem}_{number}{ext}"
        number += 1
    return path


def free_port(preferred=8770):
    for port in (preferred, 0):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            p = s.getsockname()[1]
            s.close()
            return p
        except OSError:
            continue
    return 8770


def serve(open_browser=True, port=None, ffmpeg_hint=None):
    ff = find_ffmpeg(ffmpeg_hint)
    STATE["ffmpeg"] = ff
    if ff:
        probe_path = os.path.join(os.path.dirname(ff),
                                  "ffprobe.exe" if IS_WIN else "ffprobe")
        STATE["ffprobe"] = probe_path if os.path.isfile(probe_path) else "ffprobe"

    port = port or free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"

    print(f"\nttcut {VERSION}")
    print(f"介面      {url}")
    print(f"ffmpeg    {ff or '找不到——可以標記與匯出 JSON，但無法渲染'}")
    if not ff:
        print("          Mac: brew install ffmpeg")
        print("          Windows: 把 ffmpeg.exe 放在本腳本旁邊")
    print("\n只監聽 127.0.0.1，不對外開放。按 Ctrl-C 結束。\n")

    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n結束。")
    finally:
        httpd.server_close()


# ─────────────────────────────────────────── 命令列渲染（與 V1.22 行為相同）

def cli_render(args):
    doc = json.load(open(args.tags, encoding="utf-8"))
    opt = dict(lead=args.lead, tail=args.tail, min_cut=args.min_cut,
               cut_lets=args.cut_lets, let_tail=args.let_tail,
               quality=args.quality, encoder=args.encoder, crf=args.crf,
               preset=args.preset, bitrate=args.bitrate, fps=args.fps,
               hdr=args.hdr, size=args.size, hwaccel=args.hwaccel, font=args.font,
               accent=args.accent, stats=args.stats, stats_hold=args.stats_hold,
               scoreboard_style=args.scoreboard_style)

    pl = plan(doc, opt)
    out = args.out or unique_default_out(args.video, doc.get("intro"))

    print(f"\nttcut {VERSION}")
    if not pl["ok"]:
        sys.exit(pl["reason"])
    for line in summary_lines(pl, opt, args.video)[:5]:
        print(line)
    print()
    for line in summary_lines(pl, opt, args.video)[5:]:
        print(line)
    if pl["dropped"]:
        print(f"\n略過 {len(pl['dropped'])} 個過短剪點（留著比跳接好）:")
        for f, t, why in pl["dropped"]:
            print(f"   {ts(f)} → {ts(t)}   {t - f:.2f}s   {why}")

    if args.dry_run:
        print("\n保留片段:")
        for i, (s, e) in enumerate(pl["keeps"]):
            print(f"   {i + 1:2d}  {ts(s)} → {ts(e)}   {e - s:6.2f}s")
        print()
        return

    ffmpeg = find_ffmpeg(args.ffmpeg, os.path.dirname(os.path.abspath(args.video)))
    if not ffmpeg:
        sys.exit("\n找不到 ffmpeg。\n"
                 "  Mac    : brew install ffmpeg\n"
                 "  Windows: 把 ffmpeg.exe 放到這個腳本旁邊，或用 "
                 "--ffmpeg \"C:\\ffmpeg\\bin\" 指定位置")
    ffprobe = os.path.join(os.path.dirname(ffmpeg),
                           "ffprobe.exe" if IS_WIN else "ffprobe")
    if not os.path.isfile(ffprobe):
        ffprobe = "ffprobe"
    print(f"ffmpeg    {ffmpeg}")
    print()

    cmd, workdir, flt = build_render(doc, pl, args.video, out, opt,
                                     ffmpeg, ffprobe, log=print)
    print("\n" + " ".join(
        (c if len(c) < 60 else f"<濾鏡 {len(c)} 字元，見 {flt}>") for c in cmd) + "\n")
    subprocess.run(cmd, check=True, cwd=workdir)
    intro = doc.get("intro") or {}
    if intro.get("enabled") and intro.get("thumbnail"):
        ass_name = os.path.basename(os.path.splitext(out)[0]) + ".intro.ass"
        font = select_font(intro.get("font", ""))
        subprocess.run(thumbnail_command(ffmpeg, args.video, out, ass_name,
                                         font_directory(font)), check=True, cwd=workdir)
        print(f"YouTube 封面 → {thumbnail_path(out)}")
    print(f"\n完成 → {out}")


def main():
    for stream in (sys.stdout, sys.stderr):      # Windows 主控台預設非 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(
        description=f"ttcut {VERSION} — 桌球標記與剪輯。不給參數就開介面。")
    p.add_argument("-v", "--version", action="version", version=f"ttcut {VERSION}")
    p.add_argument("tags", nargs="?", help="標記 JSON（省略則開介面）")
    p.add_argument("video", nargs="?", help="來源影片（省略則開介面）")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--lead", type=float, default=None, help="發球前保留秒數（預設讀 JSON）")
    p.add_argument("--tail", type=float, default=None, help="得分後保留秒數（預設讀 JSON）")
    p.add_argument("--min-cut", type=float, default=DEFAULT_MIN_CUT,
                   help="短於此秒數就不剪，避免無意義跳接")
    p.add_argument("--cut-lets", action="store_true", help="重發之間的撿球也剪掉")
    p.add_argument("--let-tail", type=float, default=1.5, help="重發後保留秒數")

    g = p.add_argument_group("畫質")
    g.add_argument("--quality", choices=list(QUALITY), default="high",
                   help="fast=快、high=標準（建議）、max=極致（CPU libx264，非常慢）")
    g.add_argument("--encoder", default=None,
                   help="Mac: h264_videotoolbox / Windows: h264_nvenc, h264_qsv, "
                        "h264_amf / libx264（純 CPU，最好也最慢）")
    g.add_argument("--crf", type=int, default=None, help="覆寫品質值，越小越好")
    g.add_argument("--preset", default=None, help="libx264 的 preset")
    g.add_argument("--bitrate", default=None, help="覆寫碼率，例如 40M")
    g.add_argument("--fps", default="source", help="source＝跟著片源，或直接給數字")
    g.add_argument("--hdr", choices=["auto", "tonemap", "keep", "ignore"], default="auto")
    g.add_argument("--size", default=None, help="覆寫解析度，例如 1920x1080")
    g.add_argument("--hwaccel", default="auto",
                   help="硬體解碼：auto（Mac 用 videotoolbox）/ none / cuda / qsv")

    s = p.add_argument_group("介面")
    s.add_argument("--port", type=int, default=None, help="指定連接埠")
    s.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")

    p.add_argument("--accent", default=None,
                   help=f"計分板強調色（得分數字與側邊裝飾條），例如 \"#FF7A18\"。"
                        f"預設 {DEFAULT_ACCENT}")
    p.add_argument("--scoreboard-style", choices=["koko", "ttcut"], default=None,
                   help="影片比分板樣式；覆寫標記 JSON。舊 JSON 預設 ttcut 原版")
    p.add_argument("--stats", dest="stats", action="store_true", default=None,
                   help="片尾凍結最後一格並疊上數據統計看板")
    p.add_argument("--no-stats", dest="stats", action="store_false",
                   help="不要統計看板，覆蓋標記 JSON 裡的設定")
    p.add_argument("--stats-hold", type=float, default=None,
                   help=f"統計看板停留秒數，預設 {DEFAULT_STATS_HOLD}")
    p.add_argument("--font", default=FONT_NAME, help="計分板中文字型名稱")
    p.add_argument("--ffmpeg", default=None,
                   help="ffmpeg.exe 的路徑或所在資料夾（沒裝進 PATH 時用）")
    p.add_argument("--dry-run", action="store_true", help="只印剪接表，不渲染")
    args = p.parse_args()

    if args.tags and args.video:
        cli_render(args)
    elif args.tags or args.video:
        p.error("命令列渲染需要同時給 標記JSON 與 影片；只想開介面請不要帶參數。")
    else:
        serve(open_browser=not args.no_browser, port=args.port,
              ffmpeg_hint=args.ffmpeg)


if __name__ == "__main__":
    main()
