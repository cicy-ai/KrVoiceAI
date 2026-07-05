#!/usr/bin/env python3
"""口播工坊 · 中心队列服务(cloudshell 常驻,端口 8000)。

职责:
  ① serve 口播工坊静态页(web-koubo/index.html 及同目录静态资源,`/` 返回它)。
  ② 任务队列 API —— 只做队列,不出片、不调 produce.sh(cloudshell 没 GPU)。

架构:
  前端(krvoice.cicy-ai.com,同源)   -- POST /jobs / GET /jobs/<id> -->  本服务(队列中心)
  Colab worker(pull_worker.py,GPU) -- GET /jobs/next / POST result -->  本服务(拉任务 + 回传成片)

数据落地(默认 ~/krvoice-data,可用环境变量 KRVOICE_DATA 覆盖):
  $KRVOICE_DATA/jobs/<id>.json   任务元数据
  $KRVOICE_DATA/pub/<id>.mp4     成片

数据落地(素材库,默认 ~/cicy-ai/assets,可用环境变量 KRVOICE_ASSETS 覆盖):
  $KRVOICE_ASSETS/<文件名>        用户上传的驱动视频/声样/商家图,worker 直接 HTTP 拉取

============================ API 契约 ============================
GET  /                     -> index.html(口播工坊前端)
GET  /<静态文件>            -> web-koubo/ 同目录静态资源

GET  /assets               -> 200 [{name, kind(video|audio|image|other), size, mtime}, ...] 列出素材
POST /assets               (multipart) 字段 file=<文件>
     -> 201 {name, kind}   存到 ASSETS_DIR,同名加序号避免覆盖
GET  /assets/<file>        -> 下载素材(worker 用;只允许 basename,防路径穿越)
DELETE /assets/<file>      -> 200 {ok:true} 删除一个素材;无 -> 404

POST /jobs
     body(JSON): {text, tpl?, voice?, driver?, voice_ref?, asset_imgs?, avatar?, desc?}
     -> 201 {id, status:"queued", check:"/jobs/<id>"}      (前端提交)
     text 必填,缺失 400。
       driver:     驱动视频素材文件名(worker 从 /assets/<driver> 下载)
       voice:      "云希"|"晓晓"|"克隆"(云希/晓晓→edge-tts;克隆→声音克隆)
       voice_ref:  声样素材文件名(voice=="克隆"时用,空则回退驱动视频本人)
       asset_imgs: 商家图素材文件名数组(可空;非空走图文编排,空则纯口播)

GET  /jobs/<id>
     -> 200 {id, status:queued|running|done|failed, progress?, url?, error?, ...}
     -> 404 {error}                                          (前端轮询查状态)

GET  /jobs/next
     -> 200 <job JSON>     取一个最早的 queued 任务,标记 running + 记 lease 时间
     -> 204 (空)           没有可取任务                        (worker 拉任务)

POST /jobs/<id>/status
     body(JSON): {status?, progress?}
     -> 200 {ok:true}      更新进度/状态(worker 汇报,可选)
     -> 404 {error}

POST /jobs/<id>/result    (multipart/form-data)
     字段: status = done | failed
           file   = <成片 mp4>          (status=done 时带)
           error  = <失败原因>          (status=failed 时带)
     -> 200 {ok:true, url?}
        done  时:把上传的 mp4 存到 pub/<id>.mp4,设 job.url = "/pub/<id>.mp4"
        failed 时:记 job.error
     -> 404 {error}                                          (worker 回传结果)

GET  /pub/<file>          -> 下载成片
=================================================================

lease 超时兜底:running 任务超过 LEASE_TIMEOUT(默认 30 分钟)没结果,GET /jobs/next
时自动退回 queued(防 worker 掉线把任务卡死)。

同源部署,CORS 留 `*`(非必需,方便调试跨源)。
"""
import os, json, time, uuid, threading
from flask import Flask, request, jsonify, send_from_directory, send_file, abort

PORT = int(os.environ.get("KRVOICE_PORT", "8000"))
DATA = os.path.expanduser(os.environ.get("KRVOICE_DATA", "~/krvoice-data"))
JOBS = os.path.join(DATA, "jobs")
PUB = os.path.join(DATA, "pub")
# 素材库(用户上传的驱动视频/声样/商家图),worker 从 GET /assets/<file> 拉取。
ASSETS = os.path.expanduser(os.environ.get("KRVOICE_ASSETS", "~/cicy-ai/assets"))
WEB = os.path.dirname(os.path.abspath(__file__))          # web-koubo/ 静态目录
LEASE_TIMEOUT = int(os.environ.get("KRVOICE_LEASE_TIMEOUT", str(30 * 60)))  # 秒

os.makedirs(JOBS, exist_ok=True)
os.makedirs(PUB, exist_ok=True)
os.makedirs(ASSETS, exist_ok=True)

# 素材类型(按扩展名推断)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".amr", ".ogg", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def asset_kind(name):
    e = os.path.splitext(name)[1].lower()
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in IMAGE_EXT:
        return "image"
    return "other"


def safe_asset(name):
    """把外部传入的素材名收敛成 ASSETS 下的合法路径,防路径穿越(只允许 basename)。"""
    base = os.path.basename((name or "").replace("\\", "/"))
    if not base or base in (".", ".."):
        return None
    full = os.path.normpath(os.path.join(ASSETS, base))
    if os.path.dirname(full) != os.path.normpath(ASSETS):
        return None
    return full


def unique_asset_name(name):
    """同名加序号,避免覆盖已存在素材。"""
    base = os.path.basename((name or "file").replace("\\", "/")) or "file"
    if not os.path.exists(os.path.join(ASSETS, base)):
        return base
    stem, ext = os.path.splitext(base)
    n = 1
    while True:
        cand = f"{stem}-{n}{ext}"
        if not os.path.exists(os.path.join(ASSETS, cand)):
            return cand
        n += 1

app = Flask(__name__, static_folder=None)
lock = threading.Lock()


# ---------------- CORS(同源其实用不上,留 * 方便调试) ----------------
@app.before_request
def _preflight():
    if request.method == "OPTIONS":
        return ("", 200)


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ---------------- 任务存取(文件即状态) ----------------
def jpath(i):
    return os.path.join(JOBS, f"{i}.json")


def load(i):
    try:
        with open(jpath(i)) as f:
            return json.load(f)
    except Exception:
        return None


def save(j):
    tmp = jpath(j["id"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(j, f, ensure_ascii=False)
    os.replace(tmp, jpath(j["id"]))


def all_jobs():
    out = []
    for f in sorted(os.listdir(JOBS)):
        if f.endswith(".json"):
            j = load(f[:-5])
            if j:
                out.append(j)
    return out


# ---------------- 素材库(上传/列出/下载/删除) ----------------
@app.get("/assets")
def list_assets():
    out = []
    for f in sorted(os.listdir(ASSETS)):
        full = os.path.join(ASSETS, f)
        if os.path.isfile(full):
            st = os.stat(full)
            out.append({
                "name": f,
                "kind": asset_kind(f),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    return jsonify(out), 200


@app.post("/assets")
def upload_asset():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "缺少上传文件字段 file"}), 400
    name = unique_asset_name(f.filename)
    f.save(os.path.join(ASSETS, name))
    return jsonify({"name": name, "kind": asset_kind(name)}), 201


@app.get("/assets/<path:fname>")
def get_asset(fname):
    full = safe_asset(fname)
    if not full or not os.path.isfile(full):
        abort(404)
    return send_file(full)


@app.delete("/assets/<path:fname>")
def delete_asset(fname):
    full = safe_asset(fname)
    if not full or not os.path.isfile(full):
        abort(404)
    os.remove(full)
    return jsonify({"ok": True}), 200


# ---------------- 静态页 ----------------
@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/<path:fname>")
def static_files(fname):
    # 只 serve web-koubo/ 下的真实文件,避免和 API 路由冲突(API 都是显式路由,优先匹配)
    full = os.path.normpath(os.path.join(WEB, fname))
    if not full.startswith(WEB + os.sep):
        abort(404)
    if os.path.isfile(full):
        return send_from_directory(WEB, fname)
    abort(404)


# ---------------- 队列 API ----------------
@app.post("/jobs")
def submit():
    b = request.get_json(force=True, silent=True) or {}
    if not b.get("text"):
        return jsonify({"error": "text 必填(口播文案)"}), 400
    i = time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4]
    j = {
        "id": i,
        "status": "queued",
        "created": time.strftime("%F %T"),
        "text": b["text"],
        "tpl": b.get("tpl", "ecommerce"),
        "avatar": b.get("avatar", ""),
        "voice": b.get("voice", ""),
        "desc": b.get("desc", ""),
        "driver": b.get("driver", ""),           # 驱动视频素材文件名(worker 从 /assets 下载)
        "voice_ref": b.get("voice_ref", ""),     # 声样素材文件名(voice=="克隆"时用)
        "asset_imgs": list(b.get("asset_imgs") or []),  # 商家图素材文件名数组(可空)
    }
    with lock:
        save(j)
    return jsonify({"id": i, "status": "queued", "check": f"/jobs/{i}"}), 201


@app.get("/jobs/<i>")
def status(i):
    j = load(i)
    if not j:
        return jsonify({"error": "no such job"}), 404
    j = {k: v for k, v in j.items() if k != "log"}  # log 不回给前端
    return jsonify(j), 200


@app.get("/jobs/next")
def next_job():
    now = time.time()
    with lock:
        # lease 超时兜底:running 太久没结果的先退回 queued
        for j in all_jobs():
            if j.get("status") == "running" and (now - j.get("lease", 0)) > LEASE_TIMEOUT:
                j["status"] = "queued"
                j.pop("lease", None)
                save(j)
        # 取最早的 queued(id 前缀是时间戳,字典序≈时间序)
        cand = sorted(
            [j for j in all_jobs() if j.get("status") == "queued"],
            key=lambda x: x["id"],
        )
        if not cand:
            return ("", 204)
        j = cand[0]
        j["status"] = "running"
        j["lease"] = now
        j["started"] = time.strftime("%F %T")
        save(j)
        return jsonify(j), 200


@app.post("/jobs/<i>/status")
def report_status(i):
    j = load(i)
    if not j:
        return jsonify({"error": "no such job"}), 404
    b = request.get_json(force=True, silent=True) or {}
    with lock:
        j = load(i)
        if b.get("status"):
            j["status"] = b["status"]
        if b.get("progress") is not None:
            j["progress"] = b["progress"]
        j["lease"] = time.time()  # 汇报进度也算续租
        save(j)
    return jsonify({"ok": True}), 200


@app.post("/jobs/<i>/result")
def report_result(i):
    j = load(i)
    if not j:
        return jsonify({"error": "no such job"}), 404
    st = (request.form.get("status") or "").strip()
    with lock:
        j = load(i)
        j["finished"] = time.strftime("%F %T")
        j.pop("lease", None)
        if st == "done":
            f = request.files.get("file")
            if not f:
                j.update(status="failed", error="worker 报 done 但没带成片文件")
                save(j)
                return jsonify({"error": "missing file"}), 400
            out = f"{i}.mp4"
            f.save(os.path.join(PUB, out))
            j.update(status="done", file=out, url=f"/pub/{out}", progress=100)
        else:
            j.update(status="failed", error=(request.form.get("error") or "worker 报失败")[:2000])
        save(j)
    return jsonify({"ok": True, "url": j.get("url")}), 200


@app.get("/pub/<path:p>")
def pub(p):
    full = os.path.normpath(os.path.join(PUB, p))
    if not full.startswith(PUB + os.sep) or not os.path.isfile(full):
        abort(404)
    return send_file(full, mimetype="video/mp4")


if __name__ == "__main__":
    print(f"口播工坊队列中心 :{PORT}  数据目录 {DATA}", flush=True)
    app.run(host="0.0.0.0", port=PORT)
