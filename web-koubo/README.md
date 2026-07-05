# 口播工坊 · 中心队列架构

前端和队列**同源**(都在 `krvoice.cicy-ai.com`),Colab 当拉取式 GPU worker 主动拉任务。
前端完全不知道 Colab 在哪,无需 CORS、无需配任何外部地址。

```
口播工坊前端(krvoice.cicy-ai.com,同源)
  --POST /jobs 提交-->  队列中心 server.py(cloudshell,常驻,:8000)
Colab worker pull_worker.py(GPU)
  --GET /jobs/next 拉--> server.py --produce.sh 出片--> POST /jobs/<id>/result 回传成片
前端 --GET /jobs/<id> 轮询--> done 后播 /pub/<id>.mp4
```

## 三个文件

| 文件 | 跑在哪 | 干什么 |
|------|--------|--------|
| `web-koubo/server.py` | cloudshell(无 GPU) | serve 口播工坊静态页 + 任务队列 API,**不出片**。任务/成片存本地 `~/krvoice-data`。 |
| `colab/pull_worker.py` | Colab(有 GPU) | 循环 `GET /jobs/next` 拉任务,调 `produce.sh` 出片,`POST /jobs/<id>/result` 回传成片。 |
| `web-koubo/index.html` | 浏览器(同源) | 口播工坊前端,直接打同源相对路径 `/jobs`、`/jobs/<id>`、`/pub/<id>.mp4`。 |

> 旧的 `colab/worker.py`(队列+worker 合一)已被上面两者取代,保留仅作参考。

## API 契约(server.py)

所有响应带 `Access-Control-Allow-Origin: *`(同源其实用不上,方便调试)。

| 方法 · 路径 | 谁调 | body / 返回 |
|---|---|---|
| `GET /` | 浏览器 | 返回 `index.html` |
| `GET /<静态文件>` | 浏览器 | web-koubo/ 同目录静态资源 |
| `POST /jobs` | 前端提交 | body(JSON)`{text, tpl?, avatar?, voice?, desc?, use_assets?, driver?}` → `201 {id, status:"queued", check}`;`text` 必填,缺 → `400` |
| `GET /jobs/<id>` | 前端轮询 | → `200 {id, status:queued\|running\|done\|failed, progress?, url?, error?, ...}`;无 → `404`(`log` 字段不下发) |
| `GET /jobs/next` | worker 拉 | → `200 <job JSON>`(取最早 queued,标记 running + 记 lease);无 → `204` |
| `POST /jobs/<id>/status` | worker 汇报(可选) | body(JSON)`{status?, progress?}` → `200 {ok:true}`;无 job → `404` |
| `POST /jobs/<id>/result` | worker 回传 | multipart:`status=done\|failed`,done 带 `file=<mp4>`,failed 带 `error=<原因>` → `200 {ok:true, url?}`。done 时成片存 `pub/<id>.mp4`,`job.url="/pub/<id>.mp4"` |
| `GET /pub/<file>` | 浏览器/worker | 下载成片(video/mp4) |

**lease 超时兜底**:running 任务超过 30 分钟(`KRVOICE_LEASE_TIMEOUT` 秒可调)没结果,
下次 `GET /jobs/next` 时自动退回 queued,防 worker 掉线卡死。

**环境变量**:
- `KRVOICE_DATA`(默认 `~/krvoice-data`)—— 任务/成片存放目录。
- `KRVOICE_PORT`(默认 `8000`)。
- `KRVOICE_LEASE_TIMEOUT`(默认 `1800` 秒)。

## 部署

### 1. cloudshell 起队列中心

```bash
cd ~/projects/KrVoiceAI
pip install flask
python web-koubo/server.py          # 监听 0.0.0.0:8000,静态页 + 队列 API
# 数据落地默认 ~/krvoice-data(jobs/*.json + pub/*.mp4)
```

把 `krvoice.cicy-ai.com` 反代/隧道到本机 `:8000`(前端和队列因此同源)。

### 2. Colab 起拉取式 worker

Colab 里(已装好 produce.sh 全套 GPU 环境、挂好 Drive `latentsync/`):

```bash
cd /content/KrVoiceAI       # 仓库路径
pip install requests
QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
```

`QUEUE_URL` 默认就是 `https://krvoice.cicy-ai.com`,可不设。worker 会:
从 Drive `latentsync/` 取最新驱动视频+声样;声音选「云希/晓晓」走 edge-tts 通用音色,
其余走 CosyVoice 克隆;出 `/content/final.mp4` 后回传。

> 也可让 `produce.sh` 出片后顺手拉起(设 `WORKER_AUTOSTART=1`,默认关);别再用旧 `worker.py`。

### 3. 端到端测

1. 浏览器开 `https://krvoice.cicy-ai.com` → 选形象/声音/模板 + 写文案 → 点「生成视频」。
2. 前端 `POST /jobs`,弹窗进度条随真实 `status` 走。
3. Colab worker `GET /jobs/next` 领到任务 → 出片 → `POST /jobs/<id>/result`。
4. 前端轮询到 `done`,弹窗播放 `/pub/<id>.mp4` 并可下载。

无 Colab 时快速验队列(不出片):

```bash
curl -X POST localhost:8000/jobs -H 'Content-Type: application/json' -d '{"text":"测试"}'
curl localhost:8000/jobs/next        # 模拟 worker 领任务(变 running)
curl -F status=done -F file=@some.mp4 localhost:8000/jobs/<id>/result
curl localhost:8000/jobs/<id>        # 应为 done,带 url=/pub/<id>.mp4
```
