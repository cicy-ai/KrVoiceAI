#!/bin/bash
# LatentSync 一键出片（Colab GPU）
# 用法: bash colab/latentsync.sh <视频路径> [口播文案]
# 例:   bash colab/latentsync.sh /content/VID_20260703_141258.mp4
set -e

VIDEO="${1:-/content/VID_20260703_141258.mp4}"
TEXT="${2:-大家好，欢迎观看这条由数字人生成的口播视频，效果测试。}"

if [ ! -f "$VIDEO" ]; then echo "❌ 找不到视频: $VIDEO"; exit 1; fi

cd /content
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

# 2) 模型权重（只下一次）。Colab 直连 huggingface.co 又快又稳，不走镜像
unset HF_ENDPOINT
if [ ! -f checkpoints/latentsync_unet.pt ]; then
  echo "== 下载模型(~5GB) =="
  huggingface-cli download ByteDance/LatentSync-1.6 whisper/tiny.pt --local-dir checkpoints
  huggingface-cli download ByteDance/LatentSync-1.6 latentsync_unet.pt --local-dir checkpoints
fi

# 3) 输入视频 + edge-tts 口播音频
cp "$VIDEO" input_video.mp4
python - "$TEXT" <<'PY'
import sys, asyncio, edge_tts
asyncio.run(edge_tts.Communicate(sys.argv[1], "zh-CN-XiaoxiaoNeural").save("audio.mp3"))
PY
ffmpeg -y -i audio.mp3 -ar 16000 audio.wav -loglevel error

# 4) 推理：先试 512 高清，OOM/失败自动降到 256
echo "== LatentSync 出片(512高清) =="
run() {
  python -m scripts.inference \
    --unet_config_path "configs/unet/$1" \
    --inference_ckpt_path "checkpoints/latentsync_unet.pt" \
    --inference_steps 20 --guidance_scale 1.5 --enable_deepcache \
    --video_path "input_video.mp4" --audio_path "audio.wav" \
    --video_out_path "video_out.mp4"
}
run stage2_512.yaml || { echo "== 512 失败/OOM，降到 256 重试 =="; run stage2.yaml; }

echo ""
echo "✅ 完成 -> /content/LatentSync/video_out.mp4"
