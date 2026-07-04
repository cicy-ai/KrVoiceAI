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
  bash "$REPO_DIR/colab/latentsync.sh" "$VIDEO" "$TEXT"
else
  # 克隆是主推卖点,失败就失败,不降级(效果优先,不偷换大众音色)
  bash "$REPO_DIR/colab/clone.sh" "$VIDEO" "$TEXT" "$VOICE_REF"
fi
BASE="$WORKDIR/LatentSync/video_out.mp4"
[ -f "$BASE" ] || { echo "❌ 数字人底版未生成"; exit 1; }

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
