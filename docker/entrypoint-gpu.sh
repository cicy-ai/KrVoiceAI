#!/bin/bash
# GPU 容器入口:权重外挂卷(/models)缺了就自动下载一次,然后执行命令
set -e
MODELS=${MODELS_DIR:-/models}
mkdir -p "$MODELS/latentsync" "$MODELS/cosyvoice"

# LatentSync checkpoints -> 卷
if [ ! -e /opt/LatentSync/checkpoints ] || [ ! -f /opt/LatentSync/checkpoints/latentsync_unet.pt ]; then
  rm -rf /opt/LatentSync/checkpoints
  ln -sfn "$MODELS/latentsync" /opt/LatentSync/checkpoints
fi
if [ ! -f "$MODELS/latentsync/latentsync_unet.pt" ]; then
  echo "== 下载 LatentSync-1.6 权重(~5GB, 只此一次) =="
  huggingface-cli download ByteDance/LatentSync-1.6 whisper/tiny.pt      --local-dir "$MODELS/latentsync"
  huggingface-cli download ByteDance/LatentSync-1.6 latentsync_unet.pt  --local-dir "$MODELS/latentsync"
fi

# CosyVoice2 模型 -> 卷
mkdir -p /opt/CosyVoice/pretrained_models
if [ ! -e /opt/CosyVoice/pretrained_models/CosyVoice2-0.5B ]; then
  ln -sfn "$MODELS/cosyvoice/CosyVoice2-0.5B" /opt/CosyVoice/pretrained_models/CosyVoice2-0.5B
fi
if [ ! -f "$MODELS/cosyvoice/CosyVoice2-0.5B/cosyvoice2.yaml" ] && [ ! -f "$MODELS/cosyvoice/CosyVoice2-0.5B/cosyvoice.yaml" ]; then
  echo "== 下载 CosyVoice2-0.5B(~3GB, 只此一次) =="
  python - <<'PY'
from modelscope import snapshot_download
snapshot_download('iic/CosyVoice2-0.5B', local_dir='/models/cosyvoice/CosyVoice2-0.5B')
PY
fi

echo "== 权重就绪,GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '未见GPU(需 --gpus all)') =="
exec "$@"
