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

FAKE_PRODUCE=1(无 GPU 验证链路开关):
  设 FAKE_PRODUCE=1(或 true/yes/on)时,run_job 下载完素材后**跳过 produce.sh /
  LatentSync / GPU**,改用 ffmpeg 就地造一个 ~4s、720x1280、25fps 的占位 mp4 当"成片"
  (带烧字 + 一条正弦音轨),照常 post_result 回传队列中心。用于在任何装了 ffmpeg 的
  CPU 机器上验证整条队列链路闭环:拉任务→下载素材→回传成片→前端拿到片,不需要真 GPU。
  真出片逻辑完全不受影响(不设或非真值时一行不改)。

起法(Colab):
  真出片:  QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
  假出片验链路(无 GPU): FAKE_PRODUCE=1 QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
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
            sys.stdout.write(line); sys.stdout.flush()   # 前台跑时实时打到 Colab cell(不再被 subprocess 吞掉)
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


FAKE_TRUE = ("1", "true", "yes", "on")


def _fake_enabled():
    return str(os.environ.get("FAKE_PRODUCE", "")).strip().lower() in FAKE_TRUE


def _find_cjk_font():
    """找一个能显示中文的字体文件路径,找不到返回 None(不因字体失败,后面会退化成英文/纯色)。"""
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/System/Library/Fonts/PingFang.ttc",             # macOS 本地自测兜底
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    try:
        out = subprocess.run(["fc-list", ":lang=zh", "file"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            fp = line.split(":", 1)[0].strip().rstrip(":").strip()
            if fp and os.path.exists(fp):
                return fp
    except Exception:
        pass
    return None


def _find_any_font():
    """随便找一个能显示英文的字体文件,找不到返回 None。"""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    try:
        out = subprocess.run(["fc-list", "file"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            fp = line.split(":", 1)[0].strip().rstrip(":").strip()
            if fp and os.path.exists(fp):
                return fp
    except Exception:
        pass
    return None


def _ff_escape_path(p):
    """drawtext 的 fontfile/textfile 路径转义:反斜杠、冒号、单引号。"""
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _build_fake_cmds(job, workdir, final):
    """构造若干套 ffmpeg 命令(从"带烧字"到"纯色兜底"),依次尝试,保证在任意装了
    ffmpeg 的机器上都能造出一个能播放的 mp4。文本用 textfile= 传(天然避开冒号/单引号/
    中文的转义地狱),字体找不到就退化到英文,再找不到就干脆不 drawtext 只留纯色底。"""
    text20 = (str(job.get("text") or "")).strip().replace("\r", " ").replace("\n", " ")[:20]
    jid = job["id"]

    # 中文烧字文案(有中文字体时用)
    cap_cn = os.path.join(workdir, "_fake_caption_cn.txt")
    with open(cap_cn, "w", encoding="utf-8") as f:
        f.write(f"【链路验证·假成片】\n{text20}\nid: {jid}")
    # 英文兜底文案(没有中文字体时用,避免豆腐块)
    cap_en = os.path.join(workdir, "_fake_caption_en.txt")
    with open(cap_en, "w", encoding="utf-8") as f:
        f.write(f"FAKE PRODUCE OK\n{jid}")

    def base_inputs():
        # 纯色底 4s 720x1280@25 + 一条 440Hz 正弦音轨(证明音视频都在)
        return ["ffmpeg", "-hide_banner", "-y",
                "-f", "lavfi", "-i", "color=c=0x1e2430:s=720x1280:r=25:d=4",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4"]

    def encode_tail():
        return ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                "-c:a", "aac", "-shortest", "-t", "4", final]

    def drawtext_vf(font, cap_file):
        opts = [
            "textfile='%s'" % _ff_escape_path(cap_file),
            "reload=0",
            "fontcolor=white", "fontsize=44", "line_spacing=18",
            "box=1", "boxcolor=black@0.45", "boxborderw=24",
            "x=(w-text_w)/2", "y=(h-text_h)/2",
        ]
        if font:
            opts.insert(0, "fontfile='%s'" % _ff_escape_path(font))
        return "drawtext=" + ":".join(opts)

    attempts = []
    cjk = _find_cjk_font()
    anyf = _find_any_font()
    if cjk:
        attempts.append(("中文烧字", base_inputs() + ["-vf", drawtext_vf(cjk, cap_cn)] + encode_tail()))
    if anyf:
        attempts.append(("英文烧字(无中文字体)", base_inputs() + ["-vf", drawtext_vf(anyf, cap_en)] + encode_tail()))
    # drawtext 用系统默认字体(有些 ffmpeg 内置 fontconfig 能自动挑字体)
    attempts.append(("默认字体英文烧字", base_inputs() + ["-vf", drawtext_vf(None, cap_en)] + encode_tail()))
    # 最终兜底:完全不 drawtext,只有纯色底 + 音轨,保证一定出片
    attempts.append(("纯色兜底(不烧字)", base_inputs() + encode_tail()))
    return attempts


def fake_produce(job, workdir):
    """FAKE_PRODUCE=1 时的假出片:跳过 produce.sh/GPU,用 ffmpeg 造占位 final.mp4 当成片。
    复用 stream_produce 跑 ffmpeg,合并的 stdout/stderr 也会实时 worklog 回传队列中心。
    造成功后 report_status(jid,"running",95) 并返回 (True, log_tail),外层照常 post_result。"""
    jid = job["id"]
    final = os.path.join(workdir, "final.mp4")
    worklog("[worker] ⚡ FAKE_PRODUCE=1:跳过真出片(produce.sh/LatentSync/GPU),ffmpeg 造占位成片…", jid)
    report_status(jid, "running", 60)

    env = dict(os.environ)
    last_tail = ""
    for i, (desc, cmd) in enumerate(_build_fake_cmds(job, workdir, final), 1):
        worklog(f"[worker] · 假出片尝试 {i}:{desc}", jid)
        try:
            rc, last_tail = stream_produce(jid, cmd, env)
        except subprocess.TimeoutExpired:
            worklog("[worker] · 该尝试超时,换下一套 ffmpeg 命令", jid)
            continue
        except Exception as e:
            worklog(f"[worker] · 该尝试异常:{e},换下一套 ffmpeg 命令", jid)
            continue
        if rc == 0 and os.path.exists(final) and os.path.getsize(final) > 0:
            size_kb = os.path.getsize(final) // 1024
            worklog(f"[worker] ✔ 占位成片已生成 {size_kb}KB(链路验证用假成片,非真出片)", jid)
            report_status(jid, "running", 95)
            return True, last_tail
        worklog(f"[worker] · 假出片尝试 {i} 未产出(rc={rc}),换下一套", jid)
    return False, last_tail or "FAKE_PRODUCE:所有 ffmpeg 造片尝试都失败(机器上有 ffmpeg 吗?)"


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

    # FAKE_PRODUCE=1:无 GPU 验链路开关。下载完素材后、调 produce.sh 前拦截,
    # 用 ffmpeg 造占位成片当出片成功,外层照常 post_result 回传(status=done + file)。
    if _fake_enabled():
        return fake_produce(job, workdir)

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
