import os, re, json, time, asyncio, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageColor

OUT = Path("output")
MODELS = [m for m in (os.getenv("GEMINI_MODEL"), "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest") if m]
RULES = "Be honest. Never invent facts, stats, quotes or sources. Mark anything uncertain as 'unverified'. Never promise views, followers or income. "

AGENTS = {
    "manager": dict(name="Manager", role="Chief of Staff", emoji="🧭", desc="Takes your commands, assigns tasks, tracks progress, gives next steps."),
    "scout": dict(name="Scout", role="Researcher", emoji="🔭", desc="Researches trends and facts honestly. Flags what is unverified."),
    "writer": dict(name="Writer", role="Scriptwriter", emoji="✍️", desc="Hooks, scripts, titles and captions for short videos."),
    "editor": dict(name="Editor", role="Video Editor", emoji="🎬", desc="Voiceover, captions and 9:16 render (edge-tts, Pillow, ffmpeg)."),
    "uploader": dict(name="Uploader", role="Publishing Prep", emoji="📦", desc="Titles, descriptions, hashtags and a pre-upload checklist. You publish."),
}


def llm(system, prompt, search=False):
    from google import genai
    from google.genai import types
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY missing. Run: bash run.sh")
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tools = [types.Tool(google_search=types.GoogleSearch())] if search else None
    err = None
    for m in MODELS:
        for i in range(3):
            try:
                cfg = types.GenerateContentConfig(system_instruction=system, tools=tools)
                return c.models.generate_content(model=m, contents=prompt, config=cfg).text or ""
            except Exception as e:
                err, s = e, str(e)
                if tools and "429" not in s and "404" not in s:
                    tools = None  # search grounding unavailable: retry without it
                    continue
                if "429" in s:
                    time.sleep(12 * (i + 1))
                    continue
                break
    raise err


def jparse(t):
    return json.loads(re.search(r"\{.*\}|\[.*\]", t, re.S).group(0))


def manager_plan(cmd):
    t = llm("You are the Manager of a 5-agent short-video team. Agents: scout (research), writer (hooks/script/titles/captions), "
            "editor (renders a vertical video from a writer script), uploader (title/description/hashtags/checklist package). "
            "Reply ONLY JSON: {\"summary\":\"one line\",\"steps\":[{\"agent\":\"scout|writer|editor|uploader\",\"task\":\"specific task\"}],"
            "\"next\":\"clear next step for the human\"}. Use the fewest steps needed, in order. Editor needs a writer step before it.", cmd)
    return jparse(t)


def scout(task, ctx, d, o):
    out = llm("You are Scout, a research analyst. " + RULES + "Give findings as short bullets, each with confidence (high/medium/low) "
              "and what the human should verify. Under 300 words.", task + "\n\nContext:\n" + ctx["text"], search=True)
    (d / "research.md").write_text(out)
    return out


def writer(task, ctx, d, o):
    t = llm("You are Writer for 30-45s vertical videos. " + RULES + "Use only facts in the task/context. Reply ONLY JSON: "
            "{\"title\":\"\",\"hook\":\"\",\"voiceover\":\"full narration, 80-110 words, hook first\","
            "\"scenes\":[\"3-6 on-screen caption lines, max 8 words each, in order\"],\"caption\":\"\",\"hashtags\":[\"#tag\"]}. "
            "Write in the language the task asks for.", task + "\n\nContext:\n" + ctx["text"])
    j = jparse(t)
    out = json.dumps(j, indent=2, ensure_ascii=False)
    (d / "script.json").write_text(out)
    return out


def uploader(task, ctx, d, o):
    t = llm("You are Uploader. " + RULES + "Write a Markdown pre-upload package: 3 title options (<=70 chars), a description, 5-8 hashtags, "
            "and a safety checklist (fact-check claims, no income/view guarantees, disclose AI voice/visuals per platform rules, "
            "music/image licenses, no misleading title, final human review). Do NOT upload anything.", task + "\n\nContext:\n" + ctx["text"][-6000:])
    (d / "upload_package.md").write_text(t)
    return t


def font(sz):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, sz)
        except OSError:
            pass
    return ImageFont.load_default(sz)


PALS = [("#1e1b4b", "#7c3aed"), ("#0f172a", "#0ea5e9"), ("#052e16", "#22c55e"), ("#450a0a", "#f97316")]


def frame(text, i, path):
    W, H = 1080, 1920
    c1, c2 = (ImageColor.getrgb(x) for x in PALS[i % len(PALS)])
    img = Image.new("RGB", (W, H))
    dr = ImageDraw.Draw(img)
    for y in range(H):
        dr.line([(0, y), (W, y)], fill=tuple(int(c1[k] + (c2[k] - c1[k]) * y / H) for k in range(3)))
    f, lines = font(96), [""]
    for wd in text.split():
        t = (lines[-1] + " " + wd).strip()
        if dr.textlength(t, font=f) <= W - 160:
            lines[-1] = t
        else:
            lines.append(wd)
    y = H // 2 - len(lines) * 60
    for ln in lines:
        dr.text(((W - dr.textlength(ln, font=f)) / 2, y), ln, font=f, fill="white", stroke_width=4, stroke_fill="black")
        y += 120
    img.save(path)


def editor(task, ctx, d, o):
    sc = ctx.get("script")
    if not sc:
        raise RuntimeError("Editor needs a Writer script first. Ask @writer, or run the full pipeline.")
    import edge_tts
    mp3 = d / "voice.mp3"
    asyncio.run(edge_tts.Communicate(sc["voiceover"], o.get("voice", "en-US-AndrewNeural")).save(str(mp3)))
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)]))
    lines = sc.get("scenes") or [sc["hook"]]
    w = [max(1, len(x.split())) for x in lines]
    lst = []
    for i, line in enumerate(lines):
        p = d / f"s{i}.png"
        frame(line, i, p)
        lst += [f"file '{p.resolve()}'", f"duration {dur * w[i] / sum(w):.2f}"]
    lst.append(lst[-2])
    (d / "list.txt").write_text("\n".join(lst))
    out = d / "video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(d / "list.txt"), "-i", str(mp3),
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out)], check=True)
    return f"Rendered {out}"
