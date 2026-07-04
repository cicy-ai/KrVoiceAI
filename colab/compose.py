#!/usr/bin/env python3
"""编排成片引擎 v2
商家素材(主画面·运镜·转场) + 数字人(画中画讲解) + 逐字字幕 + BGM -> 宣传片
纯 FFmpeg/CPU。Phase 1 规则版:素材按数字人配音时长平均分配。
用法:
  python compose.py --avatar 数字人片.mp4 --assets 素材目录/ --template config/templates/knowledge.yaml --out final.mp4 [--bgm x.mp3]
"""
import os, sys, glob, subprocess, tempfile, argparse, json
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
ap.add_argument("--storyboard", default="")   # AI 分镜 json;给了则按分镜排素材(句子硬切),否则平均分配
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
T = 0.6

# 排镜头计划: (素材, 时长)。有分镜=按句(硬切); 否则平均分配(xfade)
emph = []   # (start, end, 强调词) 抖音式花字
if a.storyboard and os.path.exists(a.storyboard):
    shots = json.load(open(a.storyboard, encoding="utf-8")).get("shots", [])
    plan = [(assets[max(0, min(len(assets)-1, int(s.get("asset",0))))],
             max(0.8, float(s["end"])-float(s["start"]))) for s in shots]
    emph = [(float(s["start"]), float(s["end"]), s["emphasis"].strip())
            for s in shots if s.get("emphasis","").strip()]
    USE_XFADE = False
    print(f"[compose] AI 分镜: {len(plan)} 镜头(句子切), 花字 {len(emph)} 个, 配音 {total:.1f}s", flush=True)
else:
    n = len(assets); seg = max((total+(n-1)*T)/n, T+1.0)
    plan = [(src, seg) for src in assets]
    USE_XFADE = True
    print(f"[compose] 平均分配: {n} 素材×{seg:.1f}s, 转场 {T}s", flush=True)

def make_clip(i, src, d):
    out = os.path.join(work, f"c{i}.mp4")
    if is_vid(src):
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1"
        run(["ffmpeg","-y","-t",f"{d:.3f}","-i",src,"-an","-vf",vf,"-r",str(FPS),
             "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-t",f"{d:.3f}",out,"-loglevel","error"], f"A-vid{i}")
    else:
        frames = max(2, int(d*FPS)); incr = ZAMT/frames if ZEN else 0; cap = 1+(ZAMT if ZEN else 0)
        # -loop 1 -t d 提供 frames 帧, zoompan d=1(每输入帧出1帧), 否则 frames×frames 时长爆炸
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"zoompan=z='min(zoom+{incr:.6f},{cap})':d=1:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},setsar=1")
        run(["ffmpeg","-y","-loop","1","-t",f"{d:.3f}","-i",src,"-vf",vf,"-r",str(FPS),
             "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",out,"-loglevel","error"], f"A-img{i}")
    return out

clips = [make_clip(i, src, d) for i,(src,d) in enumerate(plan)]

# ---- 拼主画面 ----
main_bg = os.path.join(work, "main_bg.mp4")
if len(clips) == 1:
    run(["ffmpeg","-y","-i",clips[0],"-c","copy",main_bg,"-loglevel","error"], "A2-single")
elif USE_XFADE:
    seg_v = plan[0][1]
    ins = []
    for c in clips: ins += ["-i", c]
    fc, prev = "", "0:v"
    for i in range(1, len(clips)):
        fc += f"[{prev}][{i}:v]xfade=transition=fade:duration={T}:offset={i*(seg_v-T):.3f}[v{i}];"; prev=f"v{i}"
    run(["ffmpeg","-y",*ins,"-filter_complex",fc.rstrip(";"),"-map",f"[{prev}]",
         "-r",str(FPS),"-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",main_bg,"-loglevel","error"], "A2-xfade")
else:
    lst = os.path.join(work, "list.txt")
    open(lst,"w").write("".join(f"file '{c}'\n" for c in clips))
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",lst,"-c","copy",main_bg,"-loglevel","error"], "A2-concat")

# ---- 字幕: whisper 逐字 -> 卡拉OK ASS ----
subs = os.path.join(work, "subs.ass")
try:
    if os.environ.get("COMPOSE_NOSUB"): raise RuntimeError("skip subtitle (COMPOSE_NOSUB)")
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get("WHISPER_MODEL","small"), device="cpu", compute_type="int8")
    segs, _ = model.transcribe(a.avatar, language="zh", word_timestamps=True)
    def cs(t):
        h=int(t//3600); m=int(t%3600//60); s=t%60; return f"{h}:{m:02d}:{s:05.2f}"
    # 模板字号是小分辨率习惯值,ASS PlayRes 是 1080x1920 -> 放大 4.5 倍(16->72px 可读)
    def_fs = int(sub.get('fontsize',16) * 4.5)
    style=(f"Style: Def,Noto Sans CJK SC,{def_fs},{sub.get('primary','&H00FFFFFF')},"
           f"{sub.get('highlight','&H0000E5FF')},&H00000000,&H96000000,{sub.get('bold',1)},0,0,0,"
           f"100,100,0,0,1,{max(3,int(sub.get('outline',2))*2)},1,2,60,60,{sub.get('margin_v_compose',430)},1")
    # 花字样式:大字号、顶部居中、粗描边(抖音综艺感)
    pop_fs = int(sub.get('fontsize',16) * 8)
    pop_col = sub.get('pop_color', sub.get('highlight','&H0000E5FF'))
    pop_style=(f"Style: Pop,Noto Sans CJK SC,{pop_fs},{pop_col},{pop_col},"
               f"&H50101010,&H90000000,1,0,0,0,100,100,1,0,1,5,3,8,60,60,{sub.get('pop_margin_v',360)},1")
    lines=[]
    for s in segs:
        ws=[w for w in (s.words or [])]
        if not ws: lines.append((s.start,s.end,s.text.strip())); continue
        txt="".join("{\\k%d}%s"%(max(1,int((w.end-w.start)*100)), w.word.strip()) for w in ws)
        lines.append((ws[0].start, ws[-1].end, txt))
    pop_tag = r"{\fad(120,100)\t(0,160,\fscx124\fscy124)\t(160,320,\fscx100\fscy100)}"
    with open(subs,"w",encoding="utf-8") as f:
        f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n")
        f.write("Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n")
        f.write(style+"\n"+pop_style+"\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
        for st,en,tx in lines: f.write(f"Dialogue: 0,{cs(st)},{cs(en)},Def,,0,0,0,,{tx}\n")
        for st,en,word in emph:  # 强调词花字(层1,压在字幕上方)
            f.write(f"Dialogue: 1,{cs(st)},{cs(min(en,st+2.2))},Pop,,0,0,0,,{pop_tag}{word}\n")
    has_sub=True; print(f"[compose] 字幕 {len(lines)} 句", flush=True)
except Exception as e:
    sys.stderr.write(f"[compose] 字幕跳过: {e}\n"); has_sub=False

# ---- Stage B: 主画面 + 数字人画中画 + BGM (先不烧字幕) ----
import shutil
pip_h = int(H * PIPH)
posmap = {"br": ("W-w-40", "H-h-40"), "bl": ("40", "H-h-40")}
px, py = posmap.get(POS, posmap["br"])

comp = os.path.join(work, "composited.mp4")
inputs = ["-i", main_bg, "-i", a.avatar]
fc = f"[1:v]scale=-2:{pip_h},pad=iw+8:ih+8:4:4:white,setsar=1[pip];[0:v][pip]overlay={px}:{py}[comp]"
use_bgm = a.bgm and os.path.exists(a.bgm)
if use_bgm:
    inputs += ["-stream_loop","-1","-i",a.bgm]
    fc += f";[2:a]volume={BGV}[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    amap = "[aout]"
else:
    amap = "1:a"
run(["ffmpeg","-y",*inputs,"-filter_complex",fc,"-map","[comp]","-map",amap,
     "-r",str(FPS),"-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",
     "-c:a","aac","-shortest",comp,"-loglevel","error"], "B")

# ---- Stage C: 烧字幕(单独一步,相对路径,稳)。本机若无 libass 则跳过,不阻断 ----
burned = False
if has_sub:
    rel = "._compose_subs.ass"
    shutil.copy(subs, rel)
    r = subprocess.run(["ffmpeg","-y","-i",comp,"-vf",f"subtitles={rel}","-c:a","copy",
                        "-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",
                        a.out,"-loglevel","error"], capture_output=True, text=True)
    try: os.remove(rel)
    except OSError: pass
    if r.returncode == 0 and os.path.exists(a.out):
        burned = True
    else:
        sys.stderr.write("[compose] 烧字幕失败(可能本机 ffmpeg 无 libass),用无字幕版\n")
if not burned:
    shutil.copy(comp, a.out)

print(f"\n✅ 编排成片完成 -> {a.out} ({dur(a.out):.1f}s)", flush=True)
