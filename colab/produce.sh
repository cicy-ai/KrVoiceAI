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
BGM="${5:-}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==== 方案 ===="
echo " 人(视频): $VIDEO"
echo " 声音参考: $VOICE_REF"
echo " 文案: ${TEXT:0:30}..."
echo " 模板: $TPL"
echo "=============="

# ① 声音克隆 + ② 数字人底版（clone.sh -> $WORKDIR/LatentSync/video_out.mp4）
bash "$REPO_DIR/colab/clone.sh" "$VIDEO" "$TEXT" "$VOICE_REF"
BASE="$WORKDIR/LatentSync/video_out.mp4"
[ -f "$BASE" ] || { echo "❌ 数字人底版未生成"; exit 1; }

# ③ 逐字字幕 + ④ 成片（套行业模板）
bash "$REPO_DIR/colab/finish.sh" "$BASE" "$TPL" "$WORKDIR/final.mp4" "$BGM"

echo ""
echo "🎬 全流程完成 -> $WORKDIR/final.mp4"
