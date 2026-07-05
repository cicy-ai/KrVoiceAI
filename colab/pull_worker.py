#!/usr/bin/env python3
"""Colab 拉取式 worker(GPU + produce.sh 环境)。

循环从中心队列(cloudshell,krvoice.cicy-ai.com)拉任务出片,再把成片回传。
素材统一走阿里云 OSS(去 Google Drive、去 cloudshell 本地素材):用户在浏览器把
驱动视频 / 声样 / 商家图直传 OSS,job 里带的是 OSS key(或公读 url)。worker 用
requests 从公读 URL(https://<bucket>.<region>.aliyuncs.com/<key>)HTTP GET 下载到
本地临时目录,再跑 produce.sh。不需要 oss2、不需要 AK(公读桶,匿名 GET 即可)。

  BASE = os.environ["QUEUE_URL"]         # 默认 https://krvoice.cicy-ai.com(仅队列 API,非素材)
  OSS_PUBLIC_BASE                        # 默认 https://krvoice-assets.oss-cn-hangzhou.aliyuncs.com
  while True:
      job = GET  {BASE}/jobs/next        # 204 = 没任务,sleep 再来
      建临时工作目录 → 从 OSS 公读 URL 下载 driver / voice_ref / asset_imgs(按 key)
      产出 <workdir>/final.mp4           # 复用 produce.sh
      成功: POST {BASE}/jobs/<id>/result  multipart: status=done + file=final.mp4
      失败: POST {BASE}/jobs/<id>/result  multipart: status=failed + error=<日志尾部>
      过程中: POST {BASE}/jobs/<id>/status {status,progress}  (可选,汇报进度)
      实时日志: produce.sh 用 subprocess.Popen 逐行读,边跑边 POST {BASE}/jobs/<id>/log
                {chunk} 回传(每 ~10 行或 ~2.5s 一批),同时落地本地 /content/pull_worker.log。
                worker 不再是黑盒:任何人 curl {BASE}/jobs/<id>/log 就能实时看到干到哪。

job 字段(和前端/server 约定):
  { text, tpl, voice, driver, voice_ref, asset_imgs, desc }
    driver:     驱动视频的 OSS key(必须;也兼容直接给公读 http(s) url)
    voice:      "云希"|"晓晓"|"克隆"
                云希/晓晓 → edge-tts 通用音色(SKIP_CLONE=1 + VOICE)
                克隆      → CosyVoice 克隆,voice_ref = 下载的声样(空则回退驱动视频本人)
    voice_ref:  声样的 OSS key(voice=="克隆"时用)
    asset_imgs: 商家图 OSS key 数组(可空;非空则下载到一个目录当 produce.sh 的 ASSETS 走编排)

env:NO_PUBLISH=1(不在 worker 里发布)、WORKDIR=<临时目录>、WORKER_AUTOSTART=0。

起法(Colab):
  QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
"""
import os, sys, time, shutil, tempfile, subprocess, uuid, threading
from urllib.parse import quote, urlparse

import requests

LOG_FILE = os.environ.get("WORKER_LOG", "/content/pull_worker.log")
FLUSH_LINES = int(os.environ.get("LOG_FLUSH_LINES", "10"))   # 攒够几行就回传一次
FLUSH_SECS = float(os.environ.get("LOG_FLUSH_SECS", "2.5"))  # 或每隔几秒回传一次

BASE = os.environ.get("QUEUE_URL", "https://krvoice.cicy-ai.com").rstrip("/")
# OSS 公读下载基址(素材直传桶),job 里给 key 时据此拼公读 URL。
OSS_PUBLIC_BASE = os.environ.get(
    "OSS_PUBLIC_BASE", "https://krvoice-assets.oss-cn-hangzhou.aliyuncs.com").rstrip("/")
ROOT = os.environ.get("WORKDIR", "/content")            # 临时工作目录的父目录
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLL = int(os.environ.get("POLL_INTERVAL", "5"))          # 空闲轮询间隔(秒)
TIMEOUT = int(os.environ.get("PRODUCE_TIMEOUT", "3600"))  # 单次出片超时(秒)


def report_status(jid, status=None, progress=None):
    try:
        requests.post(f"{BASE}/jobs/{jid}/status",
                      json={"status": status, "progress": progress}, timeout=15)
    except Exception:
        pass


def report_log(jid, chunk):
    """把一批新日志实时回传队列中心(POST /jobs/<id>/log),失败静默(不阻断出片)。"""
    if not chunk:
        return
    try:
        requests.post(f"{BASE}/jobs/{jid}/log", json={"chunk": chunk}, timeout=15)
    except Exception:
        pass


def local_append(text):
    """追加到本地日志文件(方便 Colab 端直接看),失败静默。"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except Exception:
        pass


def worklog(msg, jid=None):
    """worker 自身的时间戳日志:打到 stdout + 本地文件;带 jid 时同时实时回传队列中心。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    sys.stdout.write(line); sys.stdout.flush()
    local_append(line)
    if jid:
        report_log(jid, line)


def asset_url(key_or_url):
    """job 里可能是 OSS key(assets/xxx)或直接的公读 http(s) url,统一成可下载 URL。"""
    s = str(key_or_url or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return f"{OSS_PUBLIC_BASE}/{quote(s.lstrip('/'), safe='/')}"


def download_asset(key_or_url, dest_dir):
    """从 OSS 公读 URL(或 job 给的 url)下载素材到 dest_dir,返回本地路径。"""
    url = asset_url(key_or_url)
    base = os.path.basename(urlparse(url).path) or ("asset_" + uuid.uuid4().hex[:8])
    dest = os.path.join(dest_dir, base)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                if chunk:
                    f.write(chunk)
    return dest


def stream_produce(jid, cmd, env):
    """Popen 跑 produce.sh,实时逐行读合并的 stdout/stderr:
      - 追加到本地日志文件(/content/pull_worker.log)
      - 攒够 FLUSH_LINES 行或每 FLUSH_SECS 秒,POST /jobs/<id>/log 实时回传
    返回 (returncode, log_tail)。超时抛 subprocess.TimeoutExpired 交外层处理。
    不传 cwd,沿用原 subprocess.run 的行为(工作目录由 WORKDIR 环境变量传给 produce.sh)。"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, env=env)
    tail = []                       # 只留尾部若干行给结果兜底
    buf = []                        # 待回传的一批行
    last_flush = time.time()
    timed_out = {"v": False}

    # 看门狗:即便 produce.sh 卡住不再输出(readline 阻塞),也能到点强杀,不永久挂死。
    def _kill():
        timed_out["v"] = True
        try:
            p.kill()
        except Exception:
            pass
    watchdog = threading.Timer(TIMEOUT, _kill)
    watchdog.daemon = True
    watchdog.start()

    def flush():
        if buf:
            report_log(jid, "".join(buf))
            buf.clear()

    try:
        for line in iter(p.stdout.readline, ""):
            local_append(line)
            buf.append(line)
            tail.append(line)
            if len(tail) > 400:
                del tail[:len(tail) - 400]
            now = time.time()
            if len(buf) >= FLUSH_LINES or (now - last_flush) >= FLUSH_SECS:
                flush(); last_flush = now
    finally:
        watchdog.cancel()
        try:
            p.stdout.close()
        except Exception:
            pass
    p.wait()
    flush()
    if timed_out["v"]:
        raise subprocess.TimeoutExpired(cmd, TIMEOUT)
    return p.returncode, "".join(tail)[-3000:]


def run_job(job, workdir):
    """在 workdir 里组装 produce.sh 出片,产出 <workdir>/final.mp4。返回 (ok, log_tail)。"""
    jid = job["id"]
    report_status(jid, "running", 5)

    # ① 驱动视频(必须,从素材库下载)
    drv = job.get("driver")
    if not drv:
        return False, "job 缺 driver(驱动视频素材文件名)"
    worklog("[worker] ▶ 下载素材(驱动视频/声样/商家图)...", jid)
    report_status(jid, "running", 10)
    try:
        driver = download_asset(drv, workdir)
    except Exception as e:
        return False, f"下载驱动视频 {drv} 失败: {e}"

    env = dict(os.environ, NO_PUBLISH="1", WORKDIR=workdir, WORKER_AUTOSTART="0")

    # ② 声音映射:云希/晓晓 走 edge-tts 通用音色;其余(克隆)走 CosyVoice 克隆。
    vname = job.get("voice", "") or ""
    voice_ref = ""
    if "云希" in vname:
        env["SKIP_CLONE"] = "1"; env["VOICE"] = "zh-CN-YunxiNeural"
    elif "晓晓" in vname:
        env["SKIP_CLONE"] = "1"; env["VOICE"] = "zh-CN-XiaoxiaoNeural"
    else:
        # 克隆:有声样就下载当参考,空则回退(produce.sh 默认拿驱动视频本人的声音克隆)
        ref = job.get("voice_ref") or ""
        if ref:
            try:
                voice_ref = download_asset(ref, workdir)
            except Exception as e:
                return False, f"下载声样 {ref} 失败: {e}"

    # ③ 商家图(可空):非空则下载到一个目录当 produce.sh 的 ASSETS 走图文编排。
    assets = ""
    imgs = job.get("asset_imgs") or []
    if imgs:
        imgdir = os.path.join(workdir, "assets")
        os.makedirs(imgdir, exist_ok=True)
        for nm in imgs:
            try:
                download_asset(nm, imgdir)
            except Exception as e:
                return False, f"下载商家图 {nm} 失败: {e}"
        assets = imgdir

    worklog("[worker] ✔ 素材下载完成", jid)
    final = os.path.join(workdir, "final.mp4")
    cmd = ["bash", f"{REPO}/colab/produce.sh",
           driver, job["text"], job.get("tpl", "ecommerce"),
           voice_ref, assets, "", job.get("desc", "")]
    worklog("[worker] ▶ 开始出片(装环境/克隆/对口型/编排,produce.sh 实时日志如下)...", jid)
    report_status(jid, "running", 30)
    rc, log_tail = stream_produce(jid, cmd, env)

    if rc == 0 and os.path.exists(final):
        worklog("[worker] ✔ 出片成功,准备回传成片", jid)
        report_status(jid, "running", 95)
        return True, log_tail
    worklog(f"[worker] ✖ 出片失败(rc={rc},final.mp4 {'存在' if os.path.exists(final) else '未生成'})", jid)
    return False, log_tail or f"produce.sh rc={rc},final.mp4 未生成"


def post_result(job, ok, log_tail, workdir):
    jid = job["id"]
    url = f"{BASE}/jobs/{jid}/result"
    final = os.path.join(workdir, "final.mp4")
    if ok and os.path.exists(final):
        worklog("[worker] ▶ 回传成片到队列中心...", jid)
        with open(final, "rb") as fp:
            requests.post(url, data={"status": "done"},
                          files={"file": (f"{jid}.mp4", fp, "video/mp4")}, timeout=600)
        worklog(f"[worker] ✅ {jid} 出片完成,已回传成片", jid)
    else:
        worklog(f"[worker] ❌ {jid} 失败,回传错误(详细过程日志已实时传过)", jid)
        requests.post(url, data={"status": "failed", "error": (log_tail or "未知错误")[-1500:]}, timeout=60)


def main():
    worklog(f"🛰 pull_worker 上线 -> 队列中心 {BASE}(每 {POLL}s 拉一次,素材走 OSS 公读 {OSS_PUBLIC_BASE})")
    base_dir = ROOT if os.path.isdir(ROOT) else None
    idle_beat = 0
    while True:
        try:
            r = requests.get(f"{BASE}/jobs/next", timeout=30)
        except Exception as e:
            worklog(f"⚠️ 拉任务失败(队列中心不可达?){e},{POLL}s 后重试")
            time.sleep(POLL); continue

        if r.status_code == 204 or not (r.content or b"").strip():
            idle_beat += 1
            if idle_beat % 12 == 0:      # 空闲时每隔约 1 分钟打一次心跳,证明 worker 还活着
                worklog("… 空闲,等待任务中")
            time.sleep(POLL); continue
        idle_beat = 0

        try:
            job = r.json()
        except Exception:
            time.sleep(POLL); continue
        if not job or not job.get("id"):
            time.sleep(POLL); continue

        jid = job["id"]
        worklog(f"▶️ 领到任务 {jid}:voice={job.get('voice')} tpl={job.get('tpl')} "
                f"driver={job.get('driver')} imgs={len(job.get('asset_imgs') or [])} "
                f"text={job.get('text', '')[:20]}...", jid)
        workdir = tempfile.mkdtemp(prefix=f"krv_{jid}_", dir=base_dir)
        try:
            ok, log_tail = run_job(job, workdir)
        except subprocess.TimeoutExpired:
            ok, log_tail = False, f"produce.sh 超时(>{TIMEOUT}s)"
            worklog(f"[worker] ✖ 出片超时(>{TIMEOUT}s)", jid)
        except Exception as e:
            ok, log_tail = False, f"worker 异常: {e}"
            worklog(f"[worker] ✖ worker 异常: {e}", jid)
        try:
            post_result(job, ok, log_tail, workdir)
        except Exception as e:
            worklog(f"⚠️ 回传结果失败: {e}", jid)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
