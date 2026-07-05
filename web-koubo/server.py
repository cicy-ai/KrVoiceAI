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

素材库(阿里云 OSS,已彻底去掉本地 ~/cicy-ai/assets):
  前端浏览器拿预签名 URL 直传 OSS(国内秒传,不经 cloudshell),
  worker 从公读 URL(https://<bucket>.<region>.aliyuncs.com/<key>)HTTP GET 下载。
  OSS 配置全部走环境变量,AK 绝不硬编码:
    OSS_KEY_ID / OSS_KEY_SECRET   访问密钥(部署时注入,缺失则 /oss/presign、/assets 报 500)
    OSS_ENDPOINT                  默认 https://oss-cn-hangzhou.aliyuncs.com
    OSS_BUCKET                    默认 krvoice-assets(公读桶,CORS 已开 *)
  对象 key 统一 assets/<时间戳_uuid_安全化原名>。

============================ API 契约 ============================
GET  /                     -> index.html(口播工坊前端)
GET  /<静态文件>            -> web-koubo/ 同目录静态资源

GET  /oss/presign?filename=<原名>&kind=<video|audio|image>
     -> 200 {key, put_url, get_url}   put_url=预签名 PUT(有效1小时),前端直传;get_url=公读下载
     -> 500 {error}                   OSS 未配置(缺 OSS_KEY_ID / OSS_KEY_SECRET)
GET  /assets               -> 200 [{key, name, kind(video|audio|image|other), size, url}, ...] 列 OSS assets/ 前缀
     -> 500 {error}         OSS 未配置
DELETE /assets?key=<key>   -> 200 {ok:true} 删除一个 OSS 对象;非法 key -> 400;未配置 -> 500

POST /jobs
     body(JSON): {text, tpl?, voice?, driver?, voice_ref?, asset_imgs?, avatar?, desc?}
     -> 201 {id, status:"queued", check:"/jobs/<id>"}      (前端提交)
     text 必填,缺失 400。
       driver:     驱动视频的 OSS key(worker 从公读 URL 下载)
       voice:      "云希"|"晓晓"|"克隆"(云希/晓晓→edge-tts;克隆→声音克隆)
       voice_ref:  声样的 OSS key(voice=="克隆"时用,空则回退驱动视频本人)
       asset_imgs: 商家图 OSS key 数组(可空;非空走图文编排,空则纯口播)

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
import os, re, json, time, uuid, threading
from urllib.parse import quote
from flask import Flask, request, jsonify, send_from_directory, send_file, abort

PORT = int(os.environ.get("KRVOICE_PORT", "8000"))
DATA = os.path.expanduser(os.environ.get("KRVOICE_DATA", "~/krvoice-data"))
JOBS = os.path.join(DATA, "jobs")
PUB = os.path.join(DATA, "pub")
WEB = os.path.dirname(os.path.abspath(__file__))          # web-koubo/ 静态目录
LEASE_TIMEOUT = int(os.environ.get("KRVOICE_LEASE_TIMEOUT", str(30 * 60)))  # 秒

# ---------------- OSS 配置(AK 绝不硬编码,全走环境变量) ----------------
OSS_KEY_ID = os.environ.get("OSS_KEY_ID", "")
OSS_KEY_SECRET = os.environ.get("OSS_KEY_SECRET", "")
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
OSS_BUCKET = os.environ.get("OSS_BUCKET", "krvoice-assets")
OSS_PREFIX = "assets/"                      # bucket 内素材前缀
PRESIGN_EXPIRE = 3600                        # 预签名 PUT 有效期(秒)

os.makedirs(JOBS, exist_ok=True)
os.makedirs(PUB, exist_ok=True)

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


def oss_configured():
    return bool(OSS_KEY_ID and OSS_KEY_SECRET)


def oss_bucket():
    """构造 oss2.Bucket;未配置 AK 返回 None。"""
    if not oss_configured():
        return None
    import oss2
    auth = oss2.Auth(OSS_KEY_ID, OSS_KEY_SECRET)
    return oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)


def oss_public_base():
    """公读下载基址:https://oss-cn-hangzhou.aliyuncs.com + bucket -> https://<bucket>.oss-cn-hangzhou.aliyuncs.com"""
    m = re.match(r"^(https?://)(.+)$", OSS_ENDPOINT.rstrip("/"))
    if not m:
        return f"https://{OSS_BUCKET}.oss-cn-hangzhou.aliyuncs.com"
    return f"{m.group(1)}{OSS_BUCKET}.{m.group(2)}"


def public_url(key):
    return f"{oss_public_base()}/{quote(key, safe='/')}"


def safe_key(filename):
    """原名 -> assets/<时间戳_uuid_安全化原名>,ASCII 安全,防冲突/穿越。"""
    base = os.path.basename((filename or "").replace("\\", "/")).strip() or "file"
    base = re.sub(r"[^A-Za-z0-9._\-]+", "_", base).strip("_") or "file"
    if base in (".", ".."):
        base = "file"
    return OSS_PREFIX + time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6] + "_" + base


ERR_OSS = ({"error": "OSS 未配置:缺 OSS_KEY_ID / OSS_KEY_SECRET 环境变量"}, 500)

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


# ---------------- 素材库(阿里云 OSS:预签名直传 / 列出 / 删除) ----------------
@app.get("/oss/presign")
def oss_presign():
    """前端拿 put_url 直传 OSS,拿 key/get_url 记录。"""
    bucket = oss_bucket()
    if bucket is None:
        return jsonify(ERR_OSS[0]), ERR_OSS[1]
    filename = request.args.get("filename", "")
    key = safe_key(filename)
    try:
        # slash_safe:key 里的 '/' 不做 URL 编码,签名才对得上浏览器 PUT
        put_url = bucket.sign_url("PUT", key, PRESIGN_EXPIRE, slash_safe=True)
    except Exception as e:
        return jsonify({"error": f"生成预签名 URL 失败:{e}"}), 500
    return jsonify({"key": key, "put_url": put_url, "get_url": public_url(key)}), 200


@app.get("/assets")
def list_assets():
    bucket = oss_bucket()
    if bucket is None:
        return jsonify(ERR_OSS[0]), ERR_OSS[1]
    import oss2
    out = []
    try:
        for obj in oss2.ObjectIterator(bucket, prefix=OSS_PREFIX):
            if obj.key.endswith("/"):          # 跳过目录标记对象
                continue
            name = obj.key[len(OSS_PREFIX):]
            out.append({
                "key": obj.key,
                "name": name,
                "kind": asset_kind(name),
                "size": obj.size,
                "url": public_url(obj.key),
            })
    except Exception as e:
        return jsonify({"error": f"列 OSS 对象失败:{e}"}), 500
    return jsonify(out), 200


@app.delete("/assets")
def delete_asset():
    bucket = oss_bucket()
    if bucket is None:
        return jsonify(ERR_OSS[0]), ERR_OSS[1]
    key = request.args.get("key", "")
    if not key or not key.startswith(OSS_PREFIX) or ".." in key:
        return jsonify({"error": "非法 key(必须是 assets/ 前缀)"}), 400
    try:
        bucket.delete_object(key)
    except Exception as e:
        return jsonify({"error": f"删除 OSS 对象失败:{e}"}), 500
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
        "driver": b.get("driver", ""),           # 驱动视频 OSS key(worker 从公读 URL 下载)
        "voice_ref": b.get("voice_ref", ""),     # 声样 OSS key(voice=="克隆"时用)
        "asset_imgs": list(b.get("asset_imgs") or []),  # 商家图 OSS key 数组(可空)
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
