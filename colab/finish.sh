#!/bin/bash
# 成片引擎(纯 CPU)：数字人底版 + 行业模板 -> 逐字字幕 + 假运镜 + BGM -> 成品
# 用法: bash colab/finish.sh <底版视频> <模板名> [成品输出] [bgm音频]
set -e

IN="${1:?底版视频路径}"
TPL="${2:-knowledge}"
OUT="${3:-final.mp4}"
BGM="${4:-}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TPL_FILE="$REPO_DIR/config/templates/${TPL}.yaml"
[ -f "$TPL_FILE" ] || { echo "❌ 模板不存在: $TPL_FILE"; exit 1; }

echo "== 成片: 模板=$TPL 底版=$IN =="
pip install -q faster-whisper pyyaml 2>&1 | tail -1 || true
# 中文字幕字体(否则 libass 渲染成豆腐块)
which fc-list >/dev/null 2>&1 && fc-list | grep -qi "Noto Sans CJK" || apt-get -qq -y install fonts-noto-cjk >/dev/null 2>&1 || true

# 1) 逐字字幕：faster-whisper 词级时间戳 -> 卡拉OK ASS(按模板样式)
python - "$IN" "$TPL_FILE" <<'PY'
import sys, yaml
from faster_whisper import WhisperModel
vid, tpl_file = sys.argv[1], sys.argv[2]
tpl = yaml.safe_load(open(tpl_file, encoding="utf-8"))
s = tpl.get("subtitle", {})
model = WhisperModel("small", device="cpu", compute_type="int8")
segs, _ = model.transcribe(vid, language="zh", word_timestamps=True)

def cs(t):  # 秒 -> ASS 时间 h:mm:ss.cc
    h=int(t//3600); m=int(t%3600//60); s2=t%60
    return f"{h}:{m:02d}:{s2:05.2f}"

style = (f"Style: Def,{'Noto Sans CJK SC'},{s.get('fontsize',15)},{s.get('primary','&H00FFFFFF')},"
         f"{s.get('highlight','&H0000E5FF')},&H00000000,&H64000000,{s.get('bold',1)},0,0,0,"
         f"100,100,0,0,1,{s.get('outline',2)},1,2,40,40,{s.get('margin_v',64)},1")
lines=[]
for seg in segs:
    words=[w for w in (seg.words or [])]
    if not words:
        lines.append((seg.start,seg.end,seg.text.strip())); continue
    # 卡拉OK 逐字高亮
    txt=""
    for w in words:
        dur=max(1,int((w.end-w.start)*100))
        txt+="{\\k%d}%s"%(dur,w.word.strip())
    lines.append((words[0].start,words[-1].end,txt))
with open("subs.ass","w",encoding="utf-8") as f:
    f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n")
    f.write("[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n")
    f.write(style+"\n\n")
    f.write("[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    for st,en,tx in lines:
        f.write(f"Dialogue: 0,{cs(st)},{cs(en)},Def,,0,0,0,,{tx}\n")
print("字幕 subs.ass 生成")
PY

# 2) 读模板取假运镜参数
AMOUNT=$(python -c "import yaml;print(yaml.safe_load(open('$TPL_FILE'))['zoompan'].get('amount',0.06))")
ZP_EN=$(python -c "import yaml;print(int(yaml.safe_load(open('$TPL_FILE'))['zoompan'].get('enabled',True)))")
BGV=$(python -c "import yaml;print(yaml.safe_load(open('$TPL_FILE')).get('bgm',{}).get('volume',0.12))")

# 视频尺寸
read W H < <(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0:s=' ' "$IN")
FR=$(ffprobe -v error -select_streams v -show_entries stream=r_frame_rate -of default=nk=1:nw=1 "$IN" | head -1)
FR=${FR%/*}; [ -z "$FR" ] && FR=25

# 3) 假运镜(zoompan 缓慢推近) + 烧字幕
VF="scale=${W}:${H}"
if [ "$ZP_EN" = "1" ]; then
  # 缓慢推近: 每帧微增,总量 AMOUNT
  VF="scale=8*iw:8*ih,zoompan=z='min(zoom+0.0004,1+${AMOUNT})':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=${W}x${H}:fps=${FR}"
fi
VF="${VF},subtitles=subs.ass"

echo "== FFmpeg 合成(假运镜+字幕${BGM:++BGM}) =="
if [ -n "$BGM" ] && [ -f "$BGM" ]; then
  ffmpeg -y -i "$IN" -stream_loop -1 -i "$BGM" -filter_complex \
    "[0:v]${VF}[v];[1:a]volume=${BGV}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]" \
    -map "[v]" -map "[a]" -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -shortest "$OUT" -loglevel error
else
  ffmpeg -y -i "$IN" -vf "$VF" -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac "$OUT" -loglevel error
fi
echo "✅ 成片完成 -> $OUT"
