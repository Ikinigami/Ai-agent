"""
stickman_video.py - vertical (9:16) explainer video with an animated stick man,
word-by-word subtitles and a theme.

Needs:  pip install pillow      and      ffmpeg on PATH

Quick use:
    from stickman_video import render_video
    render_video(title="Stop passive rereading",
                 script="Rereading feels productive. But it is not. Test yourself instead!",
                 out_path="out.mp4",
                 audio_path="voice.mp3",      # optional - muxed into the video
                 words=None,                  # optional [(word, start_s, end_s), ...] for exact sync
                 theme="neon")                # "neon" | "chalkboard" | "notebook"

Run `python stickman_video.py` for a demo (demo_<theme>.mp4).
"""
import math
import os
import random
import subprocess
import sys
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
LW, LH, S = 700, 800, 2          # character layer size (1x) and supersampling factor
CHAR_POS = (W // 2 - LW // 2, 420)
SUB_CY = 1480                    # subtitle vertical centre
SUB_FONT, SUB_LINE_H, SUB_MAX_W = 88, 112, 880

THEMES = {
    "neon": dict(bg1=(18, 10, 52), bg2=(104, 56, 214), ink=(255, 255, 255), accent=(255, 214, 10),
                 accent2=(0, 229, 255), line=(255, 255, 255), stroke=(22, 10, 56),
                 panel=(12, 8, 34, 175), sub=(255, 255, 255), rules=False),
    "chalkboard": dict(bg1=(20, 46, 40), bg2=(36, 80, 66), ink=(245, 245, 235), accent=(255, 226, 89),
                       accent2=(130, 220, 255), line=(245, 245, 235), stroke=(8, 22, 18),
                       panel=(0, 0, 0, 120), sub=(245, 245, 235), rules=False),
    "notebook": dict(bg1=(253, 249, 236), bg2=(243, 236, 214), ink=(30, 30, 42), accent=(232, 60, 48),
                     accent2=(40, 110, 230), line=(28, 28, 40), stroke=(255, 255, 255),
                     panel=(255, 255, 255, 215), sub=(30, 30, 42), rules=True),
}

# (right upper arm, right forearm, left upper arm, left forearm) in degrees.
# 0 = hanging down, 90 = straight out to the side, 180 = straight up.
POSES = {
    "talk":  (38, 78, -38, -78),
    "point": (122, 118, -22, -12),
    "think": (62, -168, -26, -8),
    "shrug": (52, 142, -52, -142),
    "cheer": (148, 172, -148, -172),
    "wave":  (125, 150, -22, -8),
}
POSE_CYCLE = ["point", "talk", "shrug", "talk"]


# ----------------------------------------------------------------- helpers
def mix(a, b, k):
    return tuple(int(a[i] * (1 - k) + b[i] * k) for i in range(3))


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


FONT_CANDIDATES = [
    os.environ.get("STICKMAN_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@lru_cache(maxsize=None)
def font(size):
    for p in FONT_CANDIDATES:
        if p and os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def wrap(words, fnt, max_w):
    lines, cur = [], []
    for w in words:
        if cur and fnt.getlength(" ".join(cur + [w])) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def probe_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path])
        return float(out.strip())
    except Exception:
        return None


# ----------------------------------------------------------- word timing
def estimate_words(text, total=None, wpm=150):
    """Guess (word, start, end) timings. If total is given, stretch to fit it."""
    toks = text.split()
    if not toks:
        return []
    per = 60.0 / wpm
    pauses = {".": .35, "!": .35, "?": .35, ",": .18, ";": .18, ":": .18}
    t, out = 0.35, []
    for tk in toks:
        d = per * max(0.6, min(2.0, len(tk.strip(".,!?;:")) / 5.0))
        out.append([tk, t, t + d])
        t += d + pauses.get(tk[-1], 0)
    if total:
        k = max(0.2, (total - 0.35 - 0.5) / (out[-1][2] - 0.35))
        for w in out:
            w[1] = 0.35 + (w[1] - 0.35) * k
            w[2] = 0.35 + (w[2] - 0.35) * k
    return [tuple(w) for w in out]


def align_to_script(boundaries, script):
    """edge-tts word boundaries have no punctuation: put the script's own tokens
    (with punctuation) on the boundary timings."""
    toks = script.split()
    m, n = len(boundaries), len(toks)
    if not m or not n:
        return boundaries
    out = []
    for i, tk in enumerate(toks):
        _, s, e = boundaries[min(m - 1, int(i * m / n))]
        out.append((tk, s, max(e, s + 0.08)))
    return out


async def tts_with_words(text, mp3_path, voice="en-US-AndrewNeural"):
    """Optional: make the voice-over with edge-tts and get exact word timings."""
    import edge_tts
    comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    bw = []
    with open(mp3_path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                bw.append((ch["text"], s, s + ch["duration"] / 1e7))
    return align_to_script(bw, text)


# -------------------------------------------------------------- background
def make_background(th, title):
    grad = Image.linear_gradient("L").resize((W, H))
    bg = Image.composite(Image.new("RGB", (W, H), th["bg2"]),
                         Image.new("RGB", (W, H), th["bg1"]), grad)
    d = ImageDraw.Draw(bg)
    mid = mix(th["bg1"], th["bg2"], .5)
    if th["rules"]:
        for y in range(120, H, 64):
            d.line([(0, y), (W, y)], fill=mix(th["bg2"], th["accent2"], .25), width=3)
        d.line([(110, 0), (110, H)], fill=mix(th["bg2"], th["accent"], .45), width=4)
    cx, cy = W // 2, 820
    d.ellipse([cx - 410, cy - 410, cx + 410, cy + 410], fill=mix(mid, th["accent2"], .08))
    d.ellipse([cx - 300, cy - 300, cx + 300, cy + 300], fill=mix(mid, th["accent2"], .14))
    d.rounded_rectangle([200, 1178, 880, 1192], 7, fill=mix(mid, th["line"], .35))

    # title (drawn on its own layer so floating shapes pass behind it)
    tl = Image.new("RGBA", (W, 520), (0, 0, 0, 0))
    d = ImageDraw.Draw(tl)
    for size in (104, 88, 74, 62):
        f = font(size)
        lines = wrap(title.split(), f, 920)
        if len(lines) <= 3:
            break
    y = 150
    for ln in lines:
        d.text((W // 2, y), " ".join(ln), font=f, fill=th["ink"], anchor="mt",
               stroke_width=8, stroke_fill=th["stroke"])
        y += int(size * 1.2)
    d.rounded_rectangle([W // 2 - 170, y + 14, W // 2 + 170, y + 30], 8, fill=th["accent"])
    return bg, tl


def make_decor(th, seed=7):
    rnd = random.Random(seed)
    base = mix(th["bg1"], th["bg2"], .6)
    col = mix(base, th["accent2"] if th["rules"] else th["ink"], .16)
    items = [dict(x=rnd.uniform(0, W), y=rnd.uniform(0, H), r=rnd.uniform(14, 44),
                  sp=rnd.uniform(.4, 1.2), ph=rnd.uniform(0, 6.28),
                  kind=rnd.choice(["dot", "ring", "plus"])) for _ in range(16)]
    return col, items


def draw_decor(d, col, items, t):
    for it in items:
        x = (it["x"] + 40 * math.sin(t * it["sp"] + it["ph"])) % W
        y = (it["y"] - 60 * t * it["sp"]) % (H + 100) - 50
        r = it["r"]
        if it["kind"] == "dot":
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        elif it["kind"] == "ring":
            d.ellipse([x - r, y - r, x + r, y + r], outline=col, width=6)
        else:
            d.line([(x - r, y), (x + r, y)], fill=col, width=9)
            d.line([(x, y - r), (x, y + r)], fill=col, width=9)


# --------------------------------------------------------------- stick man
def star_pts(cx, cy, r, rot=0.0):
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * .45
        a = math.pi / 5 * i - math.pi / 2 + rot
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def draw_stickman(th, t, ang, mouth, blink, pose, bounce):
    img = Image.new("RGBA", (LW * S, LH * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = lambda p: (p[0] * S, p[1] * S)
    dark = (35, 28, 48, 255)
    line = th["line"] + (255,)
    accent = th["accent"] + (255,)

    def dot(p, r, fill):
        x, y = s(p)
        d.ellipse([x - r * S, y - r * S, x + r * S, y + r * S], fill=fill)

    def limb(pts, w, col=line):
        d.line([s(p) for p in pts], fill=col, width=int(w * S), joint="curve")
        for p in pts:
            dot(p, w / 2, col)

    cx, foot_y = LW // 2, 760
    hip = (cx, 530 + bounce)
    neck = (cx, 322 + bounce)
    shoulder = (cx, neck[1] + 32)
    hr = 84
    head = (cx, neck[1] - hr)

    # ground shadow
    d.ellipse([(cx - 150) * S, 748 * S, (cx + 150) * S, 776 * S], fill=(0, 0, 0, 55))

    # legs
    ky = (hip[1] + foot_y) / 2
    for sgn in (-1, 1):
        foot = (cx + sgn * 62, foot_y)
        limb([hip, (cx + sgn * 40, ky), foot], 15)
        limb([foot, (foot[0] + sgn * 32, foot_y)], 15)

    # torso, scarf
    limb([neck, hip], 15)
    limb([(cx - 28, neck[1] + 8), (cx + 28, neck[1] + 8)], 20, accent)
    limb([(cx + 14, neck[1] + 12), (cx + 30, neck[1] + 62)], 15, accent)

    # arms
    def arm(a1, a2, L1=112, L2=108):
        e = (shoulder[0] + L1 * math.sin(math.radians(a1)), shoulder[1] + L1 * math.cos(math.radians(a1)))
        h = (e[0] + L2 * math.sin(math.radians(a2)), e[1] + L2 * math.cos(math.radians(a2)))
        return [shoulder, e, h]
    limb(arm(ang[0], ang[1]), 15)
    limb(arm(ang[2], ang[3]), 15)

    # head
    x, y = s(head)
    d.ellipse([x - hr * S, y - hr * S, x + hr * S, y + hr * S], fill=(255, 255, 255, 255),
              outline=line, width=int(12 * S))
    hx, hy = head
    for sgn in (-1, 1):
        ex, ey = hx + sgn * 30, hy - 8
        if blink:
            limb([(ex - 10, ey), (ex + 10, ey)], 5, dark)
        else:
            dot((ex + 3 * math.sin(t * .8), ey), 9, dark)
        dot((hx + sgn * 50, hy + 22), 11, (255, 170, 185, 255))
        brow_y = ey - (36 if (pose == "think" and sgn == 1) else 26)
        limb([(ex - 12, brow_y + 3 * sgn), (ex + 12, brow_y - 3 * sgn)], 5, dark)
    mx, my = hx, hy + 32
    if mouth > .08:
        hh = 6 + 22 * mouth
        d.ellipse([(mx - 21) * S, (my - hh / 2) * S, (mx + 21) * S, (my + hh / 2) * S], fill=dark)
    else:
        d.arc([(mx - 24) * S, (my - 14) * S, (mx + 24) * S, (my + 18) * S], 20, 160,
              fill=dark, width=int(6 * S))

    # props
    bob = 8 * math.sin(t * 3)
    f = font(150 * S)
    if pose in ("think", "shrug"):
        d.text(s((hx + 120, hy - 115 + bob)), "?", font=f, fill=accent, anchor="mm",
               stroke_width=6 * S, stroke_fill=th["stroke"] + (255,))
    elif pose == "point":
        d.text(s((hx - 125, hy - 110 + bob)), "!", font=f, fill=accent, anchor="mm",
               stroke_width=6 * S, stroke_fill=th["stroke"] + (255,))
    else:
        for i, (ox, oy, r) in enumerate([(-130, -100, 26), (135, -85, 20), (95, -165, 14)]):
            rr = r * (.8 + .25 * math.sin(t * 6 + i * 2))
            d.polygon([s(p) for p in star_pts(hx + ox, hy + oy + bob, rr, t * .6)], fill=accent)

    return img.resize((LW, LH), Image.BOX)


# ---------------------------------------------------------------- subtitles
def build_chunks(words, th, uppercase, max_words=4, max_chars=22):
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if (len(cur) >= max_words or sum(len(x[0]) + 1 for x in cur) > max_chars
                or w[0][-1:] in ",.;:!?\u2026"):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    f = font(SUB_FONT)
    sp = f.getlength(" ")
    chunks = []
    for gi, g in enumerate(groups):
        texts = [w[0].upper() if uppercase else w[0] for w in g]
        lines, cur, cur_w = [], [], 0.0
        for i, tx in enumerate(texts):
            wd = f.getlength(tx)
            add = wd + (sp if cur else 0)
            if cur and cur_w + add > SUB_MAX_W:
                lines.append((cur, cur_w))
                cur, cur_w, add = [], 0.0, wd
            cur.append((i, tx, wd))
            cur_w += add
        lines.append((cur, cur_w))
        maxw = max(lw for _, lw in lines)
        pad_x, pad_y = 44, 30
        pw, ph = int(maxw + 2 * pad_x), int(len(lines) * SUB_LINE_H + 2 * pad_y)
        panel = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle([0, 0, pw - 1, ph - 1], 44, fill=th["panel"])
        pos = []
        for li, (ln, lw) in enumerate(lines):
            x = pad_x + (maxw - lw) / 2
            y = pad_y + li * SUB_LINE_H + SUB_LINE_H / 2
            for i, tx, wd in ln:
                pos.append((i, tx, x, y))
                x += wd + sp
        start = g[0][1]
        last_end = g[-1][2]
        nxt = groups[gi + 1][0][1] if gi + 1 < len(groups) else None
        end = min(nxt, last_end + .45) if nxt is not None else last_end + .6
        chunks.append(dict(words=g, start=start, end=end, pw=pw, ph=ph, panel=panel, pos=pos))
    return chunks


def build_segments(chunks):
    """A new pose every two caption chunks, picked from the punctuation."""
    segs, k = [], 0
    for si, i in enumerate(range(0, len(chunks), 2)):
        last_word = chunks[min(i + 1, len(chunks) - 1)]["words"][-1][0]
        if si == 0:
            pose = "wave"
        elif i + 2 >= len(chunks):
            pose = "cheer"
        elif last_word.endswith("?"):
            pose = "think"
        elif last_word.endswith("!"):
            pose = "cheer"
        else:
            pose = POSE_CYCLE[k % len(POSE_CYCLE)]
            k += 1
        segs.append((chunks[i]["start"], pose))
    return segs


def pose_state(t, segs):
    idx = 0
    for i, (st, _) in enumerate(segs):
        if st <= t:
            idx = i
    st, cur = segs[idx]
    prev = segs[idx - 1][1] if idx else cur
    e = ease((t - st) / .3) if idx else 1.0
    return prev, cur, e


# ------------------------------------------------------------------- scene
def build_scene(title, words, total, theme="neon", uppercase=False):
    th = THEMES[theme]
    bg, title_layer = make_background(th, title)
    mid = mix(th["bg1"], th["bg2"], .5)
    decor_col, decor = make_decor(th)
    chunks = build_chunks(words, th, uppercase)
    segs = build_segments(chunks)
    track = mix(mid, th["line"], .22)
    f_sub = font(SUB_FONT)

    def speaking(t):
        return any(w[1] - .02 <= t <= w[2] + .08 for w in words)

    def frame_at(t):
        fr = bg.copy()
        d = ImageDraw.Draw(fr)
        draw_decor(d, decor_col, decor, t)
        fr.paste(title_layer, (0, 0), title_layer)

        prev, cur, e = pose_state(t, segs)
        a0, a1 = POSES[prev], POSES[cur]
        ang = [a0[i] + (a1[i] - a0[i]) * e for i in range(4)]
        talk = speaking(t)
        k = 1.0 if talk else .35
        ang[0] += 6 * math.sin(t * 3.1) * k
        ang[1] += 9 * math.sin(t * 4.3 + 1) * k
        ang[2] -= 6 * math.sin(t * 2.7 + 2) * k
        ang[3] -= 9 * math.sin(t * 3.9 + .5) * k
        if prev == cur:
            wave_w = 1.0 if cur == "wave" else 0.0
        else:
            wave_w = e if cur == "wave" else ((1 - e) if prev == "wave" else 0.0)
        ang[1] += wave_w * 28 * math.sin(t * 12)

        mouth = (.35 + .65 * abs(math.sin(t * 17))) if talk else 0
        char = draw_stickman(th, t, ang, mouth, (t % 3.4) < .12, cur, 5 * math.sin(t * 4.4))
        fr.paste(char, CHAR_POS, char)

        # subtitles
        for ch in chunks:
            if ch["start"] - .03 <= t < ch["end"]:
                p = ease((t - ch["start"]) / .14)
                px = (W - ch["pw"]) // 2
                py = SUB_CY - ch["ph"] // 2 + int((1 - p) * 46)
                fr.paste(ch["panel"], (px, py), ch["panel"])
                cur_i = max([i for i, w in enumerate(ch["words"]) if w[1] <= t] or [0])
                for i, tx, x, y in ch["pos"]:
                    d.text((px + x, py + y), tx, font=f_sub, anchor="lm",
                           fill=th["accent"] if i == cur_i else th["sub"],
                           stroke_width=6, stroke_fill=th["stroke"])
                break

        # progress bar
        d.rounded_rectangle([120, 1790, 960, 1802], 6, fill=track)
        wpx = int(840 * min(1, t / total))
        if wpx > 12:
            d.rounded_rectangle([120, 1790, 120 + wpx, 1802], 6, fill=th["accent"])
        return fr

    return frame_at


# ------------------------------------------------------------------ render
def render_video(title, script, out_path, audio_path=None, words=None,
                 theme="neon", uppercase=False, progress=None):
    if theme not in THEMES:
        raise ValueError(f"theme must be one of {list(THEMES)}")
    audio_dur = probe_duration(audio_path) if audio_path else None
    if words is None:
        words = estimate_words(script, audio_dur)
    words = [(w, float(s), float(e)) for w, s, e in words]
    total = audio_dur or (words[-1][2] + 1.0)
    frame_at = build_scene(title, words, total, theme, uppercase)

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    if audio_path:
        cmd += ["-i", audio_path, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    n = int(total * FPS)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for i in range(n):
            proc.stdin.write(frame_at(i / FPS).tobytes())
            if progress:
                progress(i + 1, n)
    finally:
        proc.stdin.close()
        proc.wait()
    return out_path


if __name__ == "__main__":
    demo = ("Rereading your notes feels productive. But it is a trap! "
            "Your brain recognizes the words, so it thinks it knows them. "
            "What should you do instead? Close the book and test yourself. "
            "Struggling to remember is how memories get built.")
    themes = sys.argv[1:] or ["neon"]
    for name in themes:
        out = f"demo_{name}.mp4"
        render_video("Stop passive rereading", demo, out, theme=name,
                     progress=lambda i, n: print(f"\r{name}: {i}/{n}", end=""))
        print("\n->", out)
