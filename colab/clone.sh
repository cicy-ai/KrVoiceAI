#!/bin/bash
# 声音克隆 + 唇形同步全流程（旗博士同款：他的脸 + 克隆他的声 + 你的文案）
# 视频抽参考音 -> whisper 转写 -> CosyVoice2 零样本克隆念新文案 -> LatentSync 对口型
# 用法: bash colab/clone.sh <视频路径> [口播文案] [参考音频(默认从视频抽)]
set -e

WORKDIR="${WORKDIR:-$([ -d /workspace ] && echo /workspace || ([ -d /content ] && echo /content || pwd))}"
VIDEO="${1:-$WORKDIR/input_video.mp4}"
TEXT="${2:-大家好，欢迎观看这条由数字人生成的口播视频，这是用我自己的声音克隆出来的。}"
REF_SRC="${3:-$VIDEO}"                       # 参考音频来源（默认用视频里那个人的声音）
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$VIDEO" ]; then echo "❌ 找不到视频: $VIDEO"; exit 1; fi

cd "$WORKDIR"

# ===== 1) 安装 CosyVoice2 =====
if [ ! -d CosyVoice ]; then
  echo "== clone CosyVoice2 =="
  git clone -q --recursive https://github.com/FunAudioLLM/CosyVoice.git
fi
cd CosyVoice
if [ ! -f .cv_deps_done ]; then
  echo "== 装 CosyVoice 依赖(约5-10分钟) =="
  pip install -q -r requirements.txt 2>&1 | tail -2 || true
  pip install -q modelscope openai-whisper 2>&1 | tail -1 || true
  touch .cv_deps_done
fi
# requirements.txt 常因个别包冲突中途中断(依赖被 || true 掩盖漏装) -> 无条件补装 Matcha-TTS + CosyVoice 全部关键依赖(幂等,已装秒过)
pip install -q hyperpyyaml hydra-core hydra-colorlog omegaconf rootutils rich einops inflect unidecode \
  conformer diffusers lightning wget onnxruntime librosa \
  pyworld soundfile matplotlib gdown pydub wetext torchcodec 2>&1 | tail -3 || true

# ===== 2) 下 CosyVoice2 模型(只下一次) =====
if [ ! -d pretrained_models/CosyVoice2-0.5B ]; then
  echo "== 下 CosyVoice2-0.5B 模型 =="
  python - <<'PY'
from modelscope import snapshot_download
snapshot_download('iic/CosyVoice2-0.5B', local_dir='pretrained_models/CosyVoice2-0.5B')
PY
fi

# ===== 3) 从视频抽 8s 参考音 + whisper 转写(得到参考文本) =====
echo "== 抽参考音 + whisper 转写 =="
ffmpeg -y -i "$REF_SRC" -t 8 -ar 16000 -ac 1 "$WORKDIR/ref.wav" -loglevel error
REF_TEXT=$(python - "$WORKDIR/ref.wav" <<'PY'
import sys, whisper
m = whisper.load_model("small")
print(m.transcribe(sys.argv[1], language="zh")["text"].strip())
PY
)
echo "参考文本: $REF_TEXT"

# ===== 4) CosyVoice2 零样本克隆：用他的声音念新文案 =====
echo "== CosyVoice2 克隆合成 =="
python - "$TEXT" "$REF_TEXT" "$WORKDIR/ref.wav" "$WORKDIR/cloned.wav" <<'PY'
import sys, torch, torchaudio
sys.path.append('third_party/Matcha-TTS')
from cosyvoice.cli.cosyvoice import CosyVoice2
from cosyvoice.utils.file_utils import load_wav
tgt, ref_text, ref_wav, out = sys.argv[1:5]
cv = CosyVoice2('pretrained_models/CosyVoice2-0.5B', load_jit=False, load_trt=False, fp16=False)
prompt = load_wav(ref_wav, 16000)
segs = [j['tts_speech'] for j in cv.inference_zero_shot(tgt, ref_text, prompt, stream=False)]
torchaudio.save(out, torch.cat(segs, dim=1), cv.sample_rate)
print("克隆音频 ->", out)
PY

# ===== 5) LatentSync：他的脸 + 克隆音 对口型 =====
echo "== 交给 LatentSync 对口型 =="
AUDIO_FILE="$WORKDIR/cloned.wav" bash "$REPO_DIR/colab/latentsync.sh" "$VIDEO" "$TEXT"
