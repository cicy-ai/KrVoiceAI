#!/usr/bin/env python3
"""编排成片引擎 v2
商家素材(主画面·运镜·转场) + 数字人(画中画讲解) + 逐字字幕 + BGM -> 宣传片
纯 FFmpeg/CPU。Phase 1 规则版:素材按数字人配音时长平均分配。
用法:
  python compose.py --avatar 数字人片.mp4 --assets 素材目录/ --template config/templates/knowledge.yaml --out final.mp4 [--bgm x.mp3]
"""
import os, sys, glob, subprocess, tempfile, argparse
import yaml

def run(cmd, tag=""):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[FFMPEG FAIL {tag}] {' '.join(cmd[:8])}...\n{r.stderr[-1200:]}\n")
        raise SystemExit(2)
    return r

def dur(p):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","default=nk=1:nw=1",p], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except: return 0.0

def is_vid(p): return p.lower().endswith((".mp4",".mov",".mkv",".webm",".avi",".m4v"))

ap = argparse.ArgumentParser()
ap.add_argument("--avatar", required=True)
ap.add_argument("--assets", required=True)
ap.add_argument("--template", required=True)
ap.add_argument("--out", default="final.mp4")
ap.add_argument("--bgm", default="")
ap.add_argument("--fps", type=int, default=30)
ap.add_argument("--w", type=int, default=1080)
ap.add_argument("--h", type=int, default=1920)
a = ap.parse_args()

tpl = yaml.safe_load(open(a.template, encoding="utf-8")) or {}
W, H, FPS = a.w, a.h, a.fps
sub = tpl.get("subtitle", {})
zp  = tpl.get("zoompan", {}); ZAMT = float(zp.get("amount", 0.06)); ZEN = bool(zp.get("enabled", True))
pipc = tpl.get("pip", {}); PIPH = float(pipc.get("height", 0.30)); POS = pipc.get("pos", "br")
BGV = float(tpl.get("bgm", {}).get("volume", 0.12))
work = tempfile.mkdtemp(prefix="compose_")

# ---- 素材列表 ----
if os.path.isdir(a.assets):
    assets = sorted([x for x in glob.glob(os.path.join(a.assets, "*"))
                     if x.lower().endswith((".jpg",".jpeg",".png",".webp",".mp4",".mov",".mkv",".webm",".m4v"))])
else:
    assets = [x.strip() for x in a.assets.split(",") if x.strip()]
if not assets:
    sys.stderr.write("无商家素材\n"); raise SystemExit(2)

total = dur(a.avatar) or (len(assets) * 3.0)
T = 0.6                      # 转场时长
n = len(assets)
seg = (total + (n - 1) * T) / n
seg = max(seg, T + 1.0)
print(f"[compose] 素材 {n} 个, 配音 {total:.1f}s, 每段 {seg:.1f}s, 转场 {T}s", flush=True)

# ---- Stage A: 每素材 -> 标准片段(seg秒 / WxH / FPS, 图片带运镜) ----
clips = []
for i, src in enumerate(assets):
    out = os.path.join(work, f"c{i}.mp4")
    if is_vid(src):
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1"
        run(["ffmpeg","-y","-t",f"{seg:.3f}","-i",src,"-an","-vf",vf,"-r",str(FPS),
             "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-t",f"{seg:.3f}",out,
             "-loglevel","error"], f"A-vid{i}")
    else:
        frames = max(2, int(seg * FPS))
        incr = ZAMT / frames if ZEN else 0
        cap = 1 + (ZAMT if ZEN else 0)
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"zoompan=z='min(zoom+{incr:.6f},{cap})':d={frames}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},setsar=1")
        run(["ffmpeg","-y","-loop","1","-t",f"{seg:.3f}","-i",src,"-vf",vf,"-r",str(FPS),
             "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",out,"-loglevel","error"], f"A-img{i}")
    clips.append(out)

# ---- Stage A2: xfade 转场拼成主画面 ----
main_bg = os.path.join(work, "main_bg.mp4")
if n == 1:
    run(["ffmpeg","-y","-i",clips[0],"-c","copy",main_bg,"-loglevel","error"], "A2-single")
else:
    ins = []
    for c in clips: ins += ["-i", c]
    fc, prev = "", "0:v"
    for i in range(1, n):
        off = i * (seg - T)
        lbl = f"v{i}"
        fc += f"[{prev}][{i}:v]xfade=transition=fade:duration={T}:offset={off:.3f}[{lbl}];"
        prev = lbl
    fc = fc.rstrip(";")
    run(["ffmpeg","-y",*ins,"-filter_complex",fc,"-map",f"[{prev}]",
         "-r",str(FPS),"-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
         main_bg,"-loglevel","error"], "A2-xfade")

# ---- 字幕: whisper 逐字 -> 卡拉OK ASS ----
subs = os.path.join(work, "subs.ass")
try:
    if os.environ.get("COMPOSE_NOSUB"): raise RuntimeError("skip subtitle (COMPOSE_NOSUB)")
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get("WHISPER_MODEL","small"), device="cpu", compute_type="int8")
    segs, _ = model.transcribe(a.avatar, language="zh", word_timestamps=True)
    def cs(t):
        h=int(t//3600); m=int(t%3600//60); s=t%60; return f"{h}:{m:02d}:{s:05.2f}"
    style=(f"Style: Def,Noto Sans CJK SC,{sub.get('fontsize',16)},{sub.get('primary','&H00FFFFFF')},"
           f"{sub.get('highlight','&H0000E5FF')},&H00000000,&H96000000,{sub.get('bold',1)},0,0,0,"
           f"100,100,0,0,1,{sub.get('outline',2)},1,2,60,60,{sub.get('margin_v_compose',430)},1")
    lines=[]
    for s in segs:
        ws=[w for w in (s.words or [])]
        if not ws: lines.append((s.start,s.end,s.text.strip())); continue
        txt="".join("{\\k%d}%s"%(max(1,int((w.end-w.start)*100)), w.word.strip()) for w in ws)
        lines.append((ws[0].start, ws[-1].end, txt))
    with open(subs,"w",encoding="utf-8") as f:
        f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n")
        f.write("Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n")
        f.write(style+"\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
        for st,en,tx in lines: f.write(f"Dialogue: 0,{cs(st)},{cs(en)},Def,,0,0,0,,{tx}\n")
    has_sub=True; print(f"[compose] 字幕 {len(lines)} 句", flush=True)
except Exception as e:
    sys.stderr.write(f"[compose] 字幕跳过: {e}\n"); has_sub=False

# ---- Stage B+C: 主画面 + 数字人画中画 + 字幕 + BGM (一次合成) ----
pip_h = int(H * PIPH);
posmap = {"br": (f"W-w-40", f"H-h-40"), "bl": ("40", "H-h-40")}
px, py = posmap.get(POS, posmap["br"])
esub = subs.replace("\\", "/").replace(":", "\\:")

inputs = ["-i", main_bg, "-i", a.avatar]
fc = f"[1:v]scale=-2:{pip_h},pad=iw+8:ih+8:4:4:white,setsar=1[pip];[0:v][pip]overlay={px}:{py}[comp]"
vlast = "comp"
if has_sub:
    fc += f";[comp]subtitles='{esub}'[comp2]"; vlast = "comp2"

use_bgm = a.bgm and os.path.exists(a.bgm)
if use_bgm:
    inputs += ["-stream_loop","-1","-i",a.bgm]
    fc += f";[2:a]volume={BGV}[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    amap = "[aout]"
else:
    amap = "1:a"

run(["ffmpeg","-y",*inputs,"-filter_complex",fc,"-map",f"[{vlast}]","-map",amap,
     "-r",str(FPS),"-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",
     "-c:a","aac","-shortest",a.out,"-loglevel","error"], "BC")

print(f"\n✅ 编排成片完成 -> {a.out} ({dur(a.out):.1f}s)", flush=True)
