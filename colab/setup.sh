#!/bin/bash
# KrVoiceAI · Colab 一键环境安装
# 在 Colab GPU 运行时执行：clone Wav2Lip、修补、下模型、装依赖
set -e
cd "$(dirname "$0")/.."          # 切到仓库根
ROOT="$(pwd)"
echo "== 仓库根: $ROOT =="

echo "== 1/4 安装 KrVoiceAI + 依赖 =="
pip install -q -e . 2>&1 | tail -1 || true
pip install -q edge-tts "librosa>=0.10" soundfile opencv-python-headless imageio-ffmpeg loguru pyyaml pydantic 2>&1 | tail -1 || true

echo "== 2/4 clone Wav2Lip 并修补 =="
if [ ! -d Wav2Lip ]; then
  git clone -q --depth 1 https://github.com/Rudrabha/Wav2Lip.git
fi
# 现代 librosa: filters.mel 必须用关键字参数
sed -i 's/librosa.filters.mel(hp.sample_rate, hp.n_fft,/librosa.filters.mel(sr=hp.sample_rate, n_fft=hp.n_fft,/' Wav2Lip/audio.py || true

echo "== 3/4 下载模型权重 =="
mkdir -p Wav2Lip/checkpoints Wav2Lip/face_detection/detection/sfd
if [ ! -s Wav2Lip/face_detection/detection/sfd/s3fd.pth ]; then
  wget -q -O Wav2Lip/face_detection/detection/sfd/s3fd.pth \
    https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth
fi
if [ ! -s Wav2Lip/checkpoints/wav2lip_gan.pth ]; then
  # 经校验可用的镜像（返回有效 torch pickle）
  wget -q -O Wav2Lip/checkpoints/wav2lip_gan.pth \
    https://hf-mirror.com/Kedreamix/Linly-Talker/resolve/main/checkpoints/wav2lip_gan.pth
fi

echo "== 4/4 校验 =="
python - <<'PY'
import torch
for p in ["Wav2Lip/face_detection/detection/sfd/s3fd.pth","Wav2Lip/checkpoints/wav2lip_gan.pth"]:
    sd=torch.load(p,map_location="cpu"); k=sd.get("state_dict",sd)
    print("  OK", p, len(k),"键")
print("CUDA available:", torch.cuda.is_available())
PY
echo "== setup done =="
