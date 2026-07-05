#!/usr/bin/env python3
"""Colab 拉取式 worker(GPU + produce.sh 环境)。

循环从中心队列(cloudshell,krvoice.cicy-ai.com)拉任务出片,再把成片回传:

  BASE = os.environ["QUEUE_URL"]         # 默认 https://krvoice.cicy-ai.com
  while True:
      job = GET  {BASE}/jobs/next        # 204 = 没任务,sleep 再来
      产出 /content/final.mp4            # 复用 produce.sh(driver/voice/assets/env 逻辑同旧 worker.py)
      成功: POST {BASE}/jobs/<id>/result  multipart: status=done + file=final.mp4
      失败: POST {BASE}/jobs/<id>/result  multipart: status=failed + error=<日志尾部>
      过程中: POST {BASE}/jobs/<id>/status {status,progress}  (可选,汇报进度)

出片逻辑与旧 colab/worker.py 的 run_job 完全一致:
  - latest_media():从 Drive latentsync/ 取最新驱动视频 + 声样。
  - 声音映射:云希 -> edge-tts zh-CN-YunxiNeural(SKIP_CLONE);晓晓 -> zh-CN-XiaoxiaoNeural;
             其余(如"我的克隆声")走 CosyVoice 克隆,voice_ref = Drive 最新声样。
  - env:NO_PUBLISH=1(不在 worker 里发布)、WORKDIR=/content、WORKER_AUTOSTART=0(别再自拉旧服务)。

起法(Colab):
  QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
"""
import os, glob, time, shutil, subprocess

import urllib.request  # 仅作可选依赖探测;实际 HTTP 走 requests
import requests

BASE = os.environ.get("QUEUE_URL", "https://krvoice.cicy-ai.com").rstrip("/")
ROOT = os.environ.get("WORKDIR", "/content")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLL = int(os.environ.get("POLL_INTERVAL", "5"))          # 空闲轮询间隔(秒)
TIMEOUT = int(os.environ.get("PRODUCE_TIMEOUT", "3600"))  # 单次出片超时(秒)


def latest_media():
    """从 Drive latentsync/ 取最新驱动视频 + 声样(和旧 worker.py 一致)。"""
    D = f"{ROOT}/drive/MyDrive/latentsync"
    vids = sorted([f for f in glob.glob(D + "/*") if f.lower().endswith((".mp4", ".mov"))],
                  key=os.path.getmtime)
    auds = sorted([f for f in glob.glob(D + "/*")
                   if f.lower().endswith((".m4a", ".mp3", ".wav", ".aac", ".amr", ".ogg"))],
                  key=os.path.getmtime)
    return (vids[-1] if vids else None), (auds[-1] if auds else (vids[-1] if vids else None))


def report_status(jid, status=None, progress=None):
    try:
        requests.post(f"{BASE}/jobs/{jid}/status",
                      json={"status": status, "progress": progress}, timeout=15)
    except Exception:
        pass


def run_job(job):
    """组装 produce.sh 出片,产出 {ROOT}/final.mp4。返回 (ok, log_tail)。"""
    jid = job["id"]
    report_status(jid, "running", 5)

    driver, voice_ref = latest_media()
    driver = job.get("driver") or driver
    if not driver:
        return False, "Drive latentsync/ 无驱动视频"

    assets = f"{ROOT}/assets" if (job.get("use_assets") and glob.glob(f"{ROOT}/assets/*")) else ""
    env = dict(os.environ, NO_PUBLISH="1", WORKDIR=ROOT, WORKER_AUTOSTART="0")

    # 声音映射(同旧 worker.py):云希/晓晓 走 edge-tts 通用音色,其余走克隆。
    vname = job.get("voice", "") or ""
    if "云希" in vname:
        env["SKIP_CLONE"] = "1"; env["VOICE"] = "zh-CN-YunxiNeural"; voice_ref = ""
    elif "晓晓" in vname:
        env["SKIP_CLONE"] = "1"; env["VOICE"] = "zh-CN-XiaoxiaoNeural"; voice_ref = ""

    final = f"{ROOT}/final.mp4"
    if os.path.exists(final):
        try: os.remove(final)
        except Exception: pass

    cmd = ["bash", f"{REPO}/colab/produce.sh",
           driver, job["text"], job.get("tpl", "ecommerce"), voice_ref, assets, "", job.get("desc", "")]
    report_status(jid, "running", 15)
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=TIMEOUT)
    log_tail = (r.stdout + r.stderr)[-3000:]

    if r.returncode == 0 and os.path.exists(final):
        # 顺手留档 Drive(失败无所谓)
        try:
            od = f"{ROOT}/drive/MyDrive/latentsync/outputs"; os.makedirs(od, exist_ok=True)
            shutil.copy(final, f"{od}/{jid}.mp4")
        except Exception:
            pass
        return True, log_tail
    return False, log_tail or f"produce.sh rc={r.returncode},final.mp4 未生成"


def post_result(job, ok, log_tail):
    jid = job["id"]
    url = f"{BASE}/jobs/{jid}/result"
    final = f"{ROOT}/final.mp4"
    if ok and os.path.exists(final):
        with open(final, "rb") as fp:
            requests.post(url, data={"status": "done"},
                          files={"file": (f"{jid}.mp4", fp, "video/mp4")}, timeout=600)
        print(f"  ✅ {jid} 出片完成,已回传")
    else:
        requests.post(url, data={"status": "failed", "error": (log_tail or "未知错误")[-1500:]}, timeout=60)
        print(f"  ❌ {jid} 失败,已回传日志尾部")


def main():
    print(f"🛰 pull_worker 上线 -> 队列中心 {BASE}(每 {POLL}s 拉一次)", flush=True)
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
        print(f"▶️ 领到任务 {jid}:voice={job.get('voice')} tpl={job.get('tpl')} text={job.get('text','')[:20]}...")
        try:
            ok, log_tail = run_job(job)
        except subprocess.TimeoutExpired:
            ok, log_tail = False, f"produce.sh 超时(>{TIMEOUT}s)"
        except Exception as e:
            ok, log_tail = False, f"worker 异常: {e}"
        try:
            post_result(job, ok, log_tail)
        except Exception as e:
            print(f"  ⚠️ 回传结果失败: {e}")


if __name__ == "__main__":
    main()
