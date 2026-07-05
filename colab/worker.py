#!/usr/bin/env python3
"""Colab GPU 常驻 worker:任务队列 + 公网 API,拉任务跑 produce.sh 出片。
由 produce.sh 末尾自动拉起(幂等)。API 经 cicy-cft/cloudflared 隧道暴露公网。

API(基地址看 produce.log 里的「任务队列 API」):
  GET  /            队列状态(json)
  POST /jobs        {"text":"文案", "tpl":"ecommerce", "avatar":"形象名", "voice":"声音名",
                     "desc":"素材描述", "use_assets":true} -> {"id":...}
  GET  /jobs/<id>   任务状态 + 成片链接
  GET  /pub/<file>  成片下载
所有响应带 CORS(Access-Control-Allow-Origin:*),OPTIONS 预检直接 200。
"""
import os, json, glob, time, uuid, threading, subprocess, shutil
from flask import Flask, request, jsonify, send_from_directory

PORT = 8189
ROOT = "/content"
JOBS = f"{ROOT}/jobs"; PUB = f"{ROOT}/pub"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(JOBS, exist_ok=True); os.makedirs(PUB, exist_ok=True)

app = Flask(__name__)
q = []; lock = threading.Lock()

@app.before_request
def _preflight():
    # CORS 预检:OPTIONS 直接放行(头由 after_request 统一补)
    from flask import request as _rq
    if _rq.method == "OPTIONS":
        return ("", 200)

@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

def jpath(i): return f"{JOBS}/{i}.json"
def load(i):
    try: return json.load(open(jpath(i)))
    except Exception: return None
def save(j): json.dump(j, open(jpath(j['id']), 'w'), ensure_ascii=False)

def latest_media():
    D = f"{ROOT}/drive/MyDrive/latentsync"
    vids = sorted([f for f in glob.glob(D+'/*') if f.lower().endswith(('.mp4','.mov'))], key=os.path.getmtime)
    auds = sorted([f for f in glob.glob(D+'/*') if f.lower().endswith(('.m4a','.mp3','.wav','.aac','.amr','.ogg'))], key=os.path.getmtime)
    return (vids[-1] if vids else None), (auds[-1] if auds else (vids[-1] if vids else None))

@app.get("/")
def index():
    with lock:
        items = [load(i) or {"id": i, "status": "?"} for i in
                 sorted(os.listdir(JOBS)) if i.endswith('.json')]
    return jsonify({"queue_len": len(q), "jobs": [{k: v for k, v in (j or {}).items() if k != 'log'} for j in
                    [json.load(open(f"{JOBS}/{f}")) for f in sorted(os.listdir(JOBS)) if f.endswith('.json')]]})

@app.post("/jobs")
def submit():
    b = request.get_json(force=True, silent=True) or {}
    if not b.get("text"): return jsonify({"error": "text 必填(口播文案)"}), 400
    i = time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4]
    j = {"id": i, "status": "queued", "created": time.strftime("%F %T"),
         "text": b["text"], "tpl": b.get("tpl", "ecommerce"),
         "desc": b.get("desc", ""), "use_assets": bool(b.get("use_assets", True)),
         "driver": b.get("driver", ""), "voice": b.get("voice", ""),
         "avatar": b.get("avatar", "")}
    save(j)
    with lock: q.append(i)
    return jsonify({"id": i, "status": "queued", "check": f"/jobs/{i}"})

@app.get("/jobs/<i>")
def status(i):
    j = load(i)
    return (jsonify(j), 200) if j else (jsonify({"error": "no such job"}), 404)

@app.get("/pub/<path:p>")
def pub(p): return send_from_directory(PUB, p)

def run_job(i):
    j = load(i)
    j["status"] = "running"; j["started"] = time.strftime("%F %T"); save(j)
    driver, voice_ref = latest_media()
    driver = j["driver"] or driver
    if not driver:
        j.update(status="failed", error="Drive latentsync/ 无驱动视频"); save(j); return
    assets = f"{ROOT}/assets" if (j["use_assets"] and glob.glob(f"{ROOT}/assets/*")) else ""
    env = dict(os.environ, NO_PUBLISH="1", WORKDIR=ROOT, WORKER_AUTOSTART="0")
    # 声音映射:云希/晓晓 走 edge-tts 通用音色(SKIP_CLONE=1 + VOICE 环境变量,由 produce.sh 透传给 latentsync.sh);
    #          其余(如"我的克隆声")走克隆,voice_ref = Drive 里最新声样(latest_media 已给)。
    vname = j.get("voice", "") or ""
    if "云希" in vname:
        env["SKIP_CLONE"] = "1"; env["VOICE"] = "zh-CN-YunxiNeural"; voice_ref = ""
    elif "晓晓" in vname:
        env["SKIP_CLONE"] = "1"; env["VOICE"] = "zh-CN-XiaoxiaoNeural"; voice_ref = ""
    cmd = ["bash", f"{REPO}/colab/produce.sh", driver, j["text"], j["tpl"], voice_ref, assets, "", j["desc"]]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)
    j["log"] = (r.stdout + r.stderr)[-3000:]
    final = f"{ROOT}/final.mp4"
    if r.returncode == 0 and os.path.exists(final):
        out = f"{i}.mp4"; shutil.copy(final, f"{PUB}/{out}")
        try:  # 顺手留档 Drive
            od = f"{ROOT}/drive/MyDrive/latentsync/outputs"; os.makedirs(od, exist_ok=True)
            shutil.copy(final, f"{od}/{out}")
        except Exception: pass
        base = ""
        if os.path.exists(f"{ROOT}/worker_url.txt"): base = open(f"{ROOT}/worker_url.txt").read().strip()
        j.update(status="done", finished=time.strftime("%F %T"), file=out,
                 url=(base + "/pub/" + out) if base else ("/pub/" + out))
    else:
        j.update(status="failed", finished=time.strftime("%F %T"), rc=r.returncode)
    save(j)

def worker_loop():
    while True:
        i = None
        with lock:
            if q: i = q.pop(0)
        if i:
            try: run_job(i)
            except Exception as e:
                j = load(i) or {"id": i}
                j.update(status="failed", error=str(e)[:300]); save(j)
        else:
            time.sleep(5)

def tunnel():
    """起一条固定隧道指到本 API(cicy-cft 优先,cloudflared 兜底),公网地址写 worker_url.txt + 打到日志"""
    log = f"{ROOT}/worker_cft.log"
    subprocess.run(["pkill", "-f", f"cft {PORT}"], capture_output=True)
    p = subprocess.Popen(f"nohup npx -y cicy-cft {PORT} > {log} 2>&1 &", shell=True)
    url = ""
    for _ in range(30):
        time.sleep(3)
        try:
            import re
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(log).read())
            if m: url = m.group(0); break
        except Exception: pass
    if not url:
        cf = f"{ROOT}/cloudflared"
        if not os.path.exists(cf):
            subprocess.run(["wget", "-q", "-O", cf,
                "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"])
            os.chmod(cf, 0o755)
        subprocess.Popen(f"nohup {cf} tunnel --url http://localhost:{PORT} > {log} 2>&1 &", shell=True)
        for _ in range(30):
            time.sleep(3)
            try:
                import re
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(log).read())
                if m: url = m.group(0); break
            except Exception: pass
    if url:
        open(f"{ROOT}/worker_url.txt", "w").write(url)
        print(f"🛰 任务队列 API: {url}   (POST {url}/jobs)", flush=True)
    else:
        print("⚠️ 隧道没起来,只能容器内访问 :%d" % PORT, flush=True)

if __name__ == "__main__":
    # 重启时把没跑完的任务捡回队列
    for f in sorted(os.listdir(JOBS)):
        if f.endswith(".json"):
            j = json.load(open(f"{JOBS}/{f}"))
            if j.get("status") in ("queued", "running"): q.append(j["id"])
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=tunnel, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
