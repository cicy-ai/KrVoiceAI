#!/bin/bash
# 全流程一键成片：一个"方案" = 人(视频) + voice(参考) + 文案 + 模板
#   -> CosyVoice 克隆声 -> LatentSync 数字人底版 -> 逐字字幕 + 假运镜 + BGM 成片
# 用法: bash colab/produce.sh <驱动视频> <文案> <模板名> [声音参考(默认视频本人)] [bgm]
set -e

WORKDIR="${WORKDIR:-$([ -d /workspace ] && echo /workspace || ([ -d /content ] && echo /content || pwd))}"
VIDEO="${1:?驱动视频路径}"
TEXT="${2:-大家好，今天分享一个实用的方法，希望对你有帮助。}"
TPL="${3:-knowledge}"
VOICE_REF="${4:-$VIDEO}"
ASSETS="${5:-}"          # 商家素材(目录 或 逗号分隔)。给了就走编排成片,不给走纯口播
BGM="${6:-}"
DESC="${7:-}"            # 素材描述(逗号分隔,和素材顺序对应),给了 AI 分镜能语义匹配
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==== 方案 ===="
echo " 人(视频): $VIDEO"
echo " 声音参考: $VOICE_REF"
echo " 文案: ${TEXT:0:30}..."
echo " 模板: $TPL"
echo "=============="

# 测试兜底：没有真驱动视频时，用 repo 自带 face.jpg 造静脸测试视频 + 纯TTS(不克隆)，
# 用于在 A100 上验证「装环境 + LatentSync 数字人 + 编排花字」全流程跑通。
if [ ! -f "$VIDEO" ]; then
  echo "⚠️ 驱动视频不存在: $VIDEO"
  echo "   → 用 colab/assets/face.jpg 造测试静脸视频(纯TTS跑通，不做声音克隆)"
  ffmpeg -y -loop 1 -i "$REPO_DIR/colab/assets/face.jpg" -t 6 -r 25 \
    -vf "scale=512:512,format=yuv420p" -an "$WORKDIR/_testdriver.mp4" -loglevel error
  VIDEO="$WORKDIR/_testdriver.mp4"; SKIP_CLONE=1
fi

# ① 声音 + ② 数字人底版 -> $WORKDIR/LatentSync/video_out.mp4
if [ "${SKIP_CLONE:-0}" = "1" ]; then
  echo "== 纯TTS(不克隆): latentsync.sh edge-tts 配音 + LatentSync 唇形同步 =="
  # VOICE 环境变量给了就透传为音色(如 zh-CN-XiaoxiaoNeural);没给则 latentsync.sh 用默认云希
  bash "$REPO_DIR/colab/latentsync.sh" "$VIDEO" "$TEXT" ${VOICE:+"$VOICE"}
else
  # 克隆是主推卖点,失败就失败,不降级(效果优先,不偷换大众音色)
  bash "$REPO_DIR/colab/clone.sh" "$VIDEO" "$TEXT" "$VOICE_REF"
fi
BASE="$WORKDIR/LatentSync/video_out.mp4"
[ -f "$BASE" ] || { echo "❌ 数字人底版未生成"; exit 1; }

# BGM 自动选择: 显式参数 > Drive latentsync/bgm.* > 自动下载免费欢快曲(CC-BY Kevin MacLeod)
if [ -z "$BGM" ] && [ -d /content ]; then
  DBGM=$(ls /content/drive/MyDrive/latentsync/bgm.* 2>/dev/null | head -1)
  if [ -n "$DBGM" ]; then
    BGM="$DBGM"; echo "== BGM: 用你 Drive 里的 $(basename "$DBGM") =="
  else
    if [ ! -s /content/bgm_default.mp3 ]; then
      for u in "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Cheery%20Monday.mp3" \
               "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Life%20of%20Riley.mp3" \
               "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wholesome.mp3"; do
        wget -q -T 30 -O /content/bgm_default.mp3 "$u" && [ "$(stat -c%s /content/bgm_default.mp3 2>/dev/null || echo 0)" -gt 200000 ] && break
        rm -f /content/bgm_default.mp3
      done
    fi
    [ -s /content/bgm_default.mp3 ] && { BGM=/content/bgm_default.mp3; echo "== BGM: 默认欢快曲(想换就往 Drive latentsync/ 放 bgm.mp3) =="; }
  fi
fi

# ③ 成片
if [ -n "$ASSETS" ]; then
  # 编排成片:商家素材为主画面 + 数字人画中画 + 字幕 + 运镜/转场 + BGM
  echo "== 编排成片(商家素材 + 数字人画中画 + AI 智能分镜) =="
  which fc-list >/dev/null 2>&1 && fc-list | grep -qi "Noto Sans CJK" || apt-get -qq -y install fonts-noto-cjk >/dev/null 2>&1 || true
  pip install -q faster-whisper pyyaml httpx 2>&1 | tail -1 || true
  # ③a AI 智能分镜:哪句配哪张素材 + 强调词(LLM;失败降级一句一镜轮询)
  python "$REPO_DIR/colab/storyboard.py" --audio "$BASE" --assets "$ASSETS" \
    --out "$WORKDIR/storyboard.json" ${DESC:+--desc "$DESC"} || true
  # ③b 按分镜编排成片
  SB=""; [ -f "$WORKDIR/storyboard.json" ] && SB="--storyboard $WORKDIR/storyboard.json"
  python "$REPO_DIR/colab/compose.py" --avatar "$BASE" --assets "$ASSETS" \
    --template "$REPO_DIR/config/templates/${TPL}.yaml" --out "$WORKDIR/final.mp4" $SB ${BGM:+--bgm "$BGM"}
else
  # 纯口播:数字人 + 字幕 + 运镜 + BGM
  bash "$REPO_DIR/colab/finish.sh" "$BASE" "$TPL" "$WORKDIR/final.mp4" "$BGM"
fi

echo ""
echo "🎬 全流程完成 -> $WORKDIR/final.mp4"

# ⑤ 发布(Colab 环境):公网直链 + Drive 公开链接 + 邮件通知(worker 队列模式下 NO_PUBLISH=1 跳过)
if [ -d /content ] && [ "${NO_PUBLISH:-0}" != "1" ]; then
  bash "$REPO_DIR/colab/publish.sh" "$WORKDIR/final.mp4" || true
  python "$REPO_DIR/colab/share_public.py" "$WORKDIR/final.mp4" "cicybot@qq.com" || true
fi

# ⑥ 新架构:队列中心在 cloudshell(server.py),Colab 只当拉取式 worker(pull_worker.py)。
#    默认不自拉 worker —— pull_worker.py 由使用者在 Colab 手动/notebook 里起(设 QUEUE_URL)。
#    需要出片后顺手拉起拉取式 worker,设 WORKER_AUTOSTART=1(幂等:已在跑就跳过)。
#    注意:旧的 colab/worker.py(队列+worker 合一)已被 server.py + pull_worker.py 取代,不再自拉。
if [ -d /content ] && [ "${WORKER_AUTOSTART:-0}" = "1" ]; then
  if ! pgrep -f "colab/pull_worker.py" >/dev/null 2>&1; then
    pip install -q requests 2>&1 | tail -1 || true
    QUEUE_URL="${QUEUE_URL:-https://krvoice.cicy-ai.com}" \
      nohup python "$REPO_DIR/colab/pull_worker.py" > /content/pull_worker.log 2>&1 &
    echo "== 拉取式 worker 已启动 -> ${QUEUE_URL:-https://krvoice.cicy-ai.com}(日志 /content/pull_worker.log) =="
  fi
fi
