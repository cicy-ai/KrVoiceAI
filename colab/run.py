#!/usr/bin/env python
"""KrVoiceAI · Colab 一键出片
用法: python colab/run.py "你的口播文案"
流程: 文案(mock) -> edge-tts 真人声 -> Wav2Lip 唇形同步(GPU) -> 竖屏合成
"""
import os, sys, shutil

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)
os.environ.setdefault("KRVOICEAI_HOME", os.path.join(ROOT, "workspace_data"))

# 把内置人脸放进形象目录（reference.* 被 gitignore，运行时拷贝）
avatar_dir = os.path.join(ROOT, "config/avatars/realface")
os.makedirs(avatar_dir, exist_ok=True)
shutil.copy(os.path.join(ROOT, "colab/assets/face.jpg"),
            os.path.join(avatar_dir, "reference.jpg"))

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else \
    "大家好，欢迎观看这条由 KrVoiceAI 在 Colab GPU 上生成的口播视频。"

from krvoiceai.core.config import get_config
cfg = get_config()
cfg.set("llm.provider", "mock")
cfg.set("tts.provider", "edge_tts")
cfg.set("tts.edge_voice", "zh-CN-XiaoxiaoNeural")
cfg.set("asr.provider", "mock")
cfg.set("avatar.provider", "wav2lip")
cfg.set("avatar.avatars_dir", "./config/avatars")
cfg.set("avatar.wav2lip.env_python", sys.executable)   # Colab 单一环境
cfg.set("avatar.wav2lip.inference_script", os.path.join(ROOT, "Wav2Lip/inference.py"))
cfg.set("avatar.wav2lip.checkpoint_path", os.path.join(ROOT, "Wav2Lip/checkpoints/wav2lip_gan.pth"))
cfg.set("avatar.wav2lip.nosmooth", True)

from krvoiceai.core.storage import Storage
from krvoiceai.core.ffmpeg_utils import FFmpegRunner
from krvoiceai.core.llm_client import LLMClient
from krvoiceai.modules.script_writer import ScriptWriter
from krvoiceai.modules.tts_engine import TTSEngine
from krvoiceai.modules.avatar_engine import AvatarEngine
from krvoiceai.modules.video_composer import VideoComposer
from krvoiceai.pipeline.orchestrator import PipelineOrchestrator
from krvoiceai.pipeline.state import JobStore

ff = FFmpegRunner()
orch = PipelineOrchestrator(job_store=JobStore(), storage=Storage())
orch.register_core_pipeline({
    "script_write": ScriptWriter(llm_client=LLMClient()),
    "tts": TTSEngine(),
    "avatar": AvatarEngine(ffmpeg=ff),
    "compose": VideoComposer(ffmpeg=ff),
})
jid = orch.submit_job(script=SCRIPT, avatar_id="realface")
ok = orch.run_job(jid)
out = (orch.get_status(jid) or {}).get("output") or {}
print("\n===== SUCCESS:", ok, "=====")
print("FINAL_VIDEO:", out.get("final_video"))
