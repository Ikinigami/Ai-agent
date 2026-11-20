import os, re, json, time, threading
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

env = Path(".env")
for l in (env.read_text().splitlines() if env.exists() else []):
    if "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

import agents as A  # after .env so GEMINI_MODEL is picked up

app = Flask(__name__)
LOCK, JOB = threading.Lock(), threading.Lock()
APPROVAL = {"event": threading.Event(), "ok": False}
S = {"agents": {k: dict(id=k, **v, status="IDLE", note="Waiting for orders", log=[], last="") for k, v in A.AGENTS.items()},
     "feed": [], "script": None}


def setst(k, status, note=None, line=None):
    with LOCK:
        a = S["agents"][k]
        a["status"] = status
        if note:
            a["note"] = note
        if line:
            a["log"] = (a["log"] + [time.strftime("%H:%M:%S ") + line])[-30:]
        S["feed"] = (S["feed"] + [f"{time.strftime('%H:%M')} {a['name']}: {line or note or status}"])[-40:]


def run_step(k, task, ctx, d, opts):
    setst(k, "WORKING", task[:90], "Started: " + task[:200])
    if k == "editor" and opts.get("approve", True):
        setst(k, "NEEDS APPROVAL", "Review the script, then approve to render", "Waiting for your approval")
        APPROVAL["ok"] = False
        APPROVAL["event"].clear()
        APPROVAL["event"].wait(3600)
        if not APPROVAL["ok"]:
            setst(k, "IDLE", "Render cancelled", "Not approved")
            return False
        setst(k, "WORKING", task[:90], "Approved, rendering")
    try:
        out = getattr(A, k)(task, ctx, d, opts)
        ctx["text"] += f"\n\n[{k}]\n{out}"
        if k == "writer":
            ctx["script"] = S["script"] = A.jparse(out)
        with LOCK:
            S["agents"][k]["last"] = out[:6000]
        setst(k, "DONE", "Done: " + task[:80], "Finished")
        return True
    except Exception as e:
        setst(k, "ERROR", str(e)[:140], "ERROR: " + str(e)[:300])
        return False


def job(cmd, opts, only):
    slug = re.sub(r"\W+", "-", cmd.lower())[:30].strip("-") or "job"
    d = A.OUT / (time.strftime("%Y%m%d-%H%M%S") + "-" + slug)
    d.mkdir(parents=True, exist_ok=True)
    ctx = {"text": json.dumps(S["script"], ensure_ascii=False) if S["script"] else "", "script": S["script"]}
    setst("manager", "WORKING", "Queued: " + cmd[:70], "Queued")
    with JOB:
        if only:
            steps, plan = [{"agent": only, "task": cmd}], {}
        else:
            with LOCK:
                for a in S["agents"].values():
                    a["status"] = "IDLE"
            setst("manager", "WORKING", "Planning: " + cmd[:70], "Planning")
            try:
                plan = A.manager_plan(cmd)
            except Exception as e:
                setst("manager", "ERROR", str(e)[:140], "ERROR: " + str(e)[:300])
                return
            steps = [s for s in plan.get("steps", []) if s.get("agent") in ("scout", "writer", "editor", "uploader")]
            setst("manager", "WORKING", plan.get("summary", "")[:90], "Plan: " + " > ".join(s["agent"] for s in steps))
        ok = True
        for i, s in enumerate(steps):
            if not only:
                setst("manager", "WORKING", f"Step {i + 1}/{len(steps)}: {s['agent']}")
            if not run_step(s["agent"], s["task"], ctx, d, opts):
                ok = False
                break
        if only:
            setst("manager", "IDLE", "Waiting for orders")
        else:
            setst("manager", "DONE" if ok else "ERROR", ("Next: " + plan.get("next", "Check the Output tab"))[:140] if ok else "Stopped. Check the agent that stopped.", "Finished" if ok else "Stopped")


@app.get("/")
def index():
    return send_from_directory(".", "index.html")


@app.get("/api/state")
def state():
    with LOCK:
        return jsonify(S)


@app.post("/api/command")
def command():
    j = request.get_json(force=True)
    cmd, only = (j.get("text") or "").strip(), j.get("agent")
    m = re.match(r"@(\w+)\s+(.*)", cmd, re.S)
    if m and m[1].lower() in A.AGENTS:
        only, cmd = m[1].lower(), m[2]
    if only == "manager":
        only = None
    if not cmd:
        return jsonify(error="empty"), 400
    opts = {"voice": j.get("voice") or "en-US-AndrewNeural", "approve": j.get("approve", True)}
    threading.Thread(target=job, args=(cmd, opts, only), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/approve")
def approve():
    APPROVAL["ok"] = bool(request.get_json(force=True).get("ok"))
    APPROVAL["event"].set()
    return jsonify(ok=True)


@app.get("/api/files")
def files():
    return jsonify([str(p.relative_to(A.OUT)) for p in sorted(A.OUT.rglob("*"), reverse=True)[:150] if p.is_file()])


@app.get("/files/<path:p>")
def get_file(p):
    return send_from_directory(A.OUT.resolve(), p)


if __name__ == "__main__":
    A.OUT.mkdir(exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), threaded=True)
