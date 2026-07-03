#!/usr/bin/env python3
"""AI 智能分镜 (Phase 2)
把数字人配音按句子(whisper 时间轴)切成镜头,LLM 决定每镜头配哪张商家素材 + 强调词。
输出 storyboard.json,供 compose.py 用(替代平均分配)。
无 LLM 或失败 -> 降级:一句一镜,素材轮询。
用法:
  python storyboard.py --audio 数字人片.mp4 --assets 素材目录/ --out storyboard.json [--desc "工厂,产品,门店"]
LLM 配置走环境变量: LLM_BASE_URL / LLM_KEY / LLM_MODEL(默认 OpenCode Zen big-pickle 免费)
"""
import os, sys, glob, json, argparse, subprocess

ap = argparse.ArgumentParser()
ap.add_argument("--audio", required=True)
ap.add_argument("--assets", required=True)
ap.add_argument("--out", default="storyboard.json")
ap.add_argument("--desc", default="")          # 素材描述(逗号分隔, 和素材顺序对应), 有则 LLM 能语义匹配
ap.add_argument("--no-llm", action="store_true")
a = ap.parse_args()

# 素材数
if os.path.isdir(a.assets):
    assets = sorted([x for x in glob.glob(os.path.join(a.assets,"*"))
                     if x.lower().endswith((".jpg",".jpeg",".png",".webp",".mp4",".mov",".mkv",".webm",".m4v"))])
else:
    assets = [x.strip() for x in a.assets.split(",") if x.strip()]
N = len(assets)
descs = [d.strip() for d in a.desc.split(",")] if a.desc else []

# 1) whisper 句子级时间轴
from faster_whisper import WhisperModel
model = WhisperModel(os.environ.get("WHISPER_MODEL","small"), device="cpu", compute_type="int8")
segs, _ = model.transcribe(a.audio, language="zh")
sents = [{"start": round(s.start,2), "end": round(s.end,2), "text": s.text.strip()} for s in segs if s.text.strip()]
if not sents:
    sys.stderr.write("无字幕句子\n"); raise SystemExit(2)
print(f"[storyboard] {len(sents)} 句, {N} 素材", flush=True)

def fallback():
    return [{**s, "asset": i % N, "emphasis": ""} for i, s in enumerate(sents)]

shots = None
if not a.no_llm:
    try:
        import httpx
        base = os.environ.get("LLM_BASE_URL","https://opencode.ai/zen/v1")
        key  = os.environ.get("LLM_KEY","public")
        mdl  = os.environ.get("LLM_MODEL","big-pickle")
        asset_lines = "\n".join(f"  {i}: {descs[i] if i<len(descs) else '素材'+str(i)}" for i in range(N))
        sent_lines  = "\n".join(f"  第{i}句({s['start']}-{s['end']}s): {s['text']}" for i,s in enumerate(sents))
        prompt = (
            "你是短视频剪辑师。下面是一条商家口播的逐句文案(带时间)和可用的商家素材。\n"
            "请为每一句分配最合适的一个素材(用编号),并挑出该句可强调的1个关键词(没有就空)。\n"
            "语义相关优先(讲产品配产品素材,讲门店配门店素材);相邻句尽量别老用同一个素材,有节奏。\n\n"
            f"素材:\n{asset_lines}\n\n文案逐句:\n{sent_lines}\n\n"
            "只输出 JSON 数组,每元素 {\"i\":句号,\"asset\":素材编号,\"emphasis\":\"关键词\"},不要解释。"
        )
        r = httpx.post(f"{base.rstrip('/')}/chat/completions",
                       headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
                       json={"model":mdl,"messages":[{"role":"user","content":prompt}],
                             "max_tokens":4000,"temperature":0.4,"stream":False}, timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        import re
        mjson = re.search(r"\[.*\]", content, re.DOTALL)
        plan = json.loads(mjson.group(0))
        by_i = {int(p["i"]): p for p in plan}
        shots = []
        for i, s in enumerate(sents):
            p = by_i.get(i, {})
            ai = int(p.get("asset", i % N))
            shots.append({**s, "asset": max(0, min(N-1, ai)), "emphasis": p.get("emphasis","") or ""})
        print(f"[storyboard] LLM 分镜成功 ({mdl})", flush=True)
    except Exception as e:
        sys.stderr.write(f"[storyboard] LLM 失败,降级一句一镜: {e}\n")
        shots = None

if shots is None:
    shots = fallback()

json.dump({"shots": shots, "n_assets": N}, open(a.out,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"[storyboard] -> {a.out}  ({len(shots)} 镜头)")
for s in shots:
    print(f"  {s['start']:>5}-{s['end']:<5} 素材#{s['asset']} {'★'+s['emphasis'] if s['emphasis'] else ''}  {s['text'][:20]}")
