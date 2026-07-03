#!/bin/bash
# LatentSync 一键出片（任意 GPU 机器：Colab / RunPod / 云 GPU 均可）
# 用法: bash latentsync.sh <视频路径> [口播文案]
# 例:   bash latentsync.sh /workspace/input_video.mp4
set -e

# 工作目录自适应：RunPod 用 /workspace，Colab 用 /content，否则当前目录
WORKDIR="${WORKDIR:-$([ -d /workspace ] && echo /workspace || ([ -d /content ] && echo /content || pwd))}"
VIDEO="${1:-$WORKDIR/input_video.mp4}"
TEXT="${2:-大家好，欢迎观看这条由数字人生成的口播视频，效果测试。}"
# 音色：男声 云希(默认) / 云健；女声 zh-CN-XiaoxiaoNeural / zh-CN-XiaoyiNeural
VOICE="${3:-zh-CN-YunxiNeural}"

if [ ! -f "$VIDEO" ]; then echo "❌ 找不到视频: $VIDEO（把视频放到 $WORKDIR/input_video.mp4，或传路径做第1个参数）"; exit 1; fi

cd "$WORKDIR"
[ -d LatentSync ] || git clone https://github.com/bytedance/LatentSync.git
cd LatentSync

# 1) 依赖（只装一次）
if [ ! -f .deps_done ]; then
  echo "== 安装依赖(约5-10分钟) =="
  apt-get -qq -y install libgl1 >/dev/null 2>&1 || true
  # 修正 Colab(Python 3.12) 上缺 wheel 的固定版本
  sed -i 's/^mediapipe==.*/mediapipe/'  requirements.txt      # 0.10.11 无 3.12 wheel
  sed -i 's/^decord==.*/eva-decord/'    requirements.txt      # decord 无 3.12 wheel，用同名 fork
  sed -i 's/^onnxruntime-gpu==.*/onnxruntime-gpu/' requirements.txt
  sed -i 's/^insightface==.*/insightface/' requirements.txt
  pip install -q -r requirements.txt
  pip install -q edge-tts
  touch .deps_done
fi

# 修复 peft/accelerate 版本冲突(clear_device_cache)——每次确保，pip 已满足则秒过
pip install -q -U "accelerate>=0.34" 2>&1 | tail -1 || true

# 2) 模型权重（只下一次）。默认直连 huggingface.co；国内(阿里云)先 export HF_ENDPOINT=https://hf-mirror.com
if [ ! -f checkpoints/latentsync_unet.pt ]; then
  echo "== 下载模型(~5GB) =="
  huggingface-cli download ByteDance/LatentSync-1.6 whisper/tiny.pt --local-dir checkpoints
  huggingface-cli download ByteDance/LatentSync-1.6 latentsync_unet.pt --local-dir checkpoints
fi

# 3) 输入视频 + edge-tts 口播音频
cp "$VIDEO" input_video.mp4
if [ -n "$AUDIO_FILE" ] && [ -f "$AUDIO_FILE" ]; then
  echo "== 使用外部音频(克隆音): $AUDIO_FILE =="
  ffmpeg -y -i "$AUDIO_FILE" -ar 16000 -ac 1 audio.wav -loglevel error
else
  echo "== 生成音频 voice=$VOICE (edge-tts 通用音) =="
  python - "$TEXT" "$VOICE" <<'PY'
import sys, asyncio, edge_tts
asyncio.run(edge_tts.Communicate(sys.argv[1], sys.argv[2]).save("audio.mp3"))
PY
  ffmpeg -y -i audio.mp3 -ar 16000 audio.wav -loglevel error
fi

# 4) 推理（LatentSync-1.6 是 512 模型，只能跑 512 配置；256 会出鬼脸，不降级）
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
echo "== LatentSync 出片(512 高清) =="
T0=$(date +%s)
echo "⏱ 出片开始: $(date '+%Y-%m-%d %H:%M:%S')"
if python -m scripts.inference \
    --unet_config_path "configs/unet/stage2_512.yaml" \
    --inference_ckpt_path "checkpoints/latentsync_unet.pt" \
    --inference_steps 20 --guidance_scale 1.5 --enable_deepcache \
    --video_path "input_video.mp4" --audio_path "audio.wav" \
    --video_out_path "video_out.mp4"; then
  ELAPSED=$(( $(date +%s) - T0 ))
  echo ""
  echo "✅ 完成 -> $WORKDIR/LatentSync/video_out.mp4"
  echo "⏱ 出片结束: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "⏱ 本条出片耗时: ${ELAPSED} 秒 ($((ELAPSED/60))分$((ELAPSED%60))秒)"
  # 视频时长，方便算"每秒视频要多久"
  DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 video_out.mp4 2>/dev/null | cut -d. -f1)
  [ -n "$DUR" ] && [ "$DUR" -gt 0 ] && echo "⏱ 成片 ${DUR}s，约 $(echo "scale=1;$ELAPSED/$DUR"|bc 2>/dev/null||echo '?') 秒/秒视频"
else
  echo ""
  echo "❌ 512 显存不足(OOM)。LatentSync-1.6 是 512 模型，T4(16GB)跑不动。"
  echo "   → 需要 24GB 显存 GPU(L4 / A10 / 3090 / 4090)。"
  echo "   ⚠️ 不要降到 256——512 权重套 256 结构会出鬼脸。"
  exit 1
fi
