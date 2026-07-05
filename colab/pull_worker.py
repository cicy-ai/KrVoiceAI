#!/usr/bin/env python3
"""Colab 拉取式 worker(GPU + produce.sh 环境)。

循环从中心队列(cloudshell,krvoice.cicy-ai.com)拉任务出片,再把成片回传。
素材统一走队列中心的素材库(去 Google Drive):worker 从 {QUEUE_URL}/assets/<文件名>
下载驱动视频 / 声样 / 商家图到本地临时目录,再跑 produce.sh。

  BASE = os.environ["QUEUE_URL"]         # 默认 https://krvoice.cicy-ai.com
  while True:
      job = GET  {BASE}/jobs/next        # 204 = 没任务,sleep 再来
      建临时工作目录 → 从 {BASE}/assets/<name> 下载 driver / voice_ref / asset_imgs
      产出 <workdir>/final.mp4           # 复用 produce.sh
      成功: POST {BASE}/jobs/<id>/result  multipart: status=done + file=final.mp4
      失败: POST {BASE}/jobs/<id>/result  multipart: status=failed + error=<日志尾部>
      过程中: POST {BASE}/jobs/<id>/status {status,progress}  (可选,汇报进度)

job 字段(和前端/server 约定):
  { text, tpl, voice, driver, voice_ref, asset_imgs, desc }
    driver:     驱动视频的素材文件名(必须,worker 从 {BASE}/assets/<driver> 下载)
    voice:      "云希"|"晓晓"|"克隆"
                云希/晓晓 → edge-tts 通用音色(SKIP_CLONE=1 + VOICE)
                克隆      → CosyVoice 克隆,voice_ref = 下载的声样(空则回退驱动视频本人)
    voice_ref:  声样文件名(voice=="克隆"时用)
    asset_imgs: 商家图文件名数组(可空;非空则下载到一个目录当 produce.sh 的 ASSETS 走编排)

env:NO_PUBLISH=1(不在 worker 里发布)、WORKDIR=<临时目录>、WORKER_AUTOSTART=0。

起法(Colab):
  QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
"""
import os, time, shutil, tempfile, subprocess
from urllib.parse import quote

import requests

BASE = os.environ.get("QUEUE_URL", "https://krvoice.cicy-ai.com").rstrip("/")
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


def download_asset(name, dest_dir):
    """从 {BASE}/assets/<name> 下载素材到 dest_dir,返回本地路径。"""
    base = os.path.basename(str(name))
    if not base:
        raise ValueError(f"非法素材名: {name!r}")
    dest = os.path.join(dest_dir, base)
    url = f"{BASE}/assets/{quote(base)}"
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                if chunk:
                    f.write(chunk)
    return dest


def run_job(job, workdir):
    """在 workdir 里组装 produce.sh 出片,产出 <workdir>/final.mp4。返回 (ok, log_tail)。"""
    jid = job["id"]
    report_status(jid, "running", 5)

    # ① 驱动视频(必须,从素材库下载)
    drv = job.get("driver")
    if not drv:
        return False, "job 缺 driver(驱动视频素材文件名)"
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

    final = os.path.join(workdir, "final.mp4")
    cmd = ["bash", f"{REPO}/colab/produce.sh",
           driver, job["text"], job.get("tpl", "ecommerce"),
           voice_ref, assets, "", job.get("desc", "")]
    report_status(jid, "running", 15)
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=TIMEOUT)
    log_tail = (r.stdout + r.stderr)[-3000:]

    if r.returncode == 0 and os.path.exists(final):
        return True, log_tail
    return False, log_tail or f"produce.sh rc={r.returncode},final.mp4 未生成"


def post_result(job, ok, log_tail, workdir):
    jid = job["id"]
    url = f"{BASE}/jobs/{jid}/result"
    final = os.path.join(workdir, "final.mp4")
    if ok and os.path.exists(final):
        with open(final, "rb") as fp:
            requests.post(url, data={"status": "done"},
                          files={"file": (f"{jid}.mp4", fp, "video/mp4")}, timeout=600)
        print(f"  ✅ {jid} 出片完成,已回传")
    else:
        requests.post(url, data={"status": "failed", "error": (log_tail or "未知错误")[-1500:]}, timeout=60)
        print(f"  ❌ {jid} 失败,已回传日志尾部")


def main():
    print(f"🛰 pull_worker 上线 -> 队列中心 {BASE}(每 {POLL}s 拉一次,素材走 {BASE}/assets)", flush=True)
    base_dir = ROOT if os.path.isdir(ROOT) else None
    while True:
        try:
            r = requests.get(f"{BASE}/jobs/next", timeout=30)
        except Exception as e:
            print(f"  ⚠️ 拉任务失败(队列中心不可达?){e},{POLL}s 后重试")
            time.sleep(POLL); continue

        if r.status_code == 204 or not (r.content or b"").strip():
            time.sleep(POLL); continue

        try:
            job = r.json()
        except Exception:
            time.sleep(POLL); continue
        if not job or not job.get("id"):
            time.sleep(POLL); continue

        jid = job["id"]
        print(f"▶️ 领到任务 {jid}:voice={job.get('voice')} tpl={job.get('tpl')} "
              f"driver={job.get('driver')} imgs={len(job.get('asset_imgs') or [])} "
              f"text={job.get('text', '')[:20]}...")
        workdir = tempfile.mkdtemp(prefix=f"krv_{jid}_", dir=base_dir)
        try:
            ok, log_tail = run_job(job, workdir)
        except subprocess.TimeoutExpired:
            ok, log_tail = False, f"produce.sh 超时(>{TIMEOUT}s)"
        except Exception as e:
            ok, log_tail = False, f"worker 异常: {e}"
        try:
            post_result(job, ok, log_tail, workdir)
        except Exception as e:
            print(f"  ⚠️ 回传结果失败: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
