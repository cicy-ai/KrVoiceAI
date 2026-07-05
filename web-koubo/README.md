# 口播工坊 · 中心队列架构

前端和队列**同源**(都在 `krvoice.cicy-ai.com`),Colab 当拉取式 GPU worker 主动拉任务。
前端完全不知道 Colab 在哪,无需 CORS、无需配任何外部地址。

**素材统一走队列中心的素材库(已彻底去掉 Google Drive)**:用户上传的驱动视频 /
声样 / 商家图都进 cloudshell 的 `~/cicy-ai/assets`(可网页上传,也可直接放文件),
worker 从 `GET /assets/<文件名>` 直接 HTTP 拉取,完全不挂 `/content/drive`。

```
口播工坊前端(krvoice.cicy-ai.com,同源)
  --POST /assets 上传素材 / GET /assets 选素材-->  队列中心 server.py(cloudshell,:8000)
  --POST /jobs 提交(带素材文件名)-------------->  队列中心
Colab worker pull_worker.py(GPU)
  --GET /jobs/next 拉--> 从 GET /assets/<name> 下载素材 --produce.sh 出片--> POST /jobs/<id>/result 回传
前端 --GET /jobs/<id> 轮询--> done 后播 /pub/<id>.mp4
```

## 三个文件

| 文件 | 跑在哪 | 干什么 |
|------|--------|--------|
| `web-koubo/server.py` | cloudshell(无 GPU) | serve 口播工坊静态页 + 任务队列 API + **素材库 API**,**不出片**。任务/成片存 `~/krvoice-data`,素材存 `~/cicy-ai/assets`。 |
| `colab/pull_worker.py` | Colab(有 GPU) | 循环 `GET /jobs/next` 拉任务,从 `GET /assets/<name>` 下载素材到临时目录,调 `produce.sh` 出片,`POST /jobs/<id>/result` 回传成片。**不再挂 Drive。** |
| `web-koubo/index.html` | 浏览器(同源) | 口播工坊前端,直接打同源相对路径 `/jobs`、`/jobs/<id>`、`/pub/<id>.mp4`。 |

> 旧的 `colab/worker.py`(队列+worker 合一)已被上面两者取代,保留仅作参考。

## API 契约(server.py)

所有响应带 `Access-Control-Allow-Origin: *`(同源其实用不上,方便调试)。

| 方法 · 路径 | 谁调 | body / 返回 |
|---|---|---|
| `GET /` | 浏览器 | 返回 `index.html` |
| `GET /<静态文件>` | 浏览器 | web-koubo/ 同目录静态资源 |
| `GET /assets` | 前端/worker | → `200 [{name, kind(video\|audio\|image\|other), size, mtime}, ...]` 列出素材库 |
| `POST /assets` | 前端上传 | multipart `file=<文件>` → `201 {name, kind}`;存到 `~/cicy-ai/assets`,同名加序号避免覆盖;无文件 → `400` |
| `GET /assets/<file>` | worker 下载 | 下载素材(只允许 basename,防路径穿越);无 → `404` |
| `DELETE /assets/<file>` | 前端 | → `200 {ok:true}` 删除一个素材;无 → `404` |
| `POST /jobs` | 前端提交 | body(JSON)`{text, tpl?, voice?, driver?, voice_ref?, asset_imgs?, avatar?, desc?}` → `201 {id, status:"queued", check}`;`text` 必填,缺 → `400`。素材字段见下表 |
| `GET /jobs/<id>` | 前端轮询 | → `200 {id, status:queued\|running\|done\|failed, progress?, url?, error?, ...}`;无 → `404`(`log` 字段不下发) |
| `GET /jobs/next` | worker 拉 | → `200 <job JSON>`(取最早 queued,标记 running + 记 lease);无 → `204` |
| `POST /jobs/<id>/status` | worker 汇报(可选) | body(JSON)`{status?, progress?}` → `200 {ok:true}`;无 job → `404` |
| `POST /jobs/<id>/result` | worker 回传 | multipart:`status=done\|failed`,done 带 `file=<mp4>`,failed 带 `error=<原因>` → `200 {ok:true, url?}`。done 时成片存 `pub/<id>.mp4`,`job.url="/pub/<id>.mp4"` |
| `GET /pub/<file>` | 浏览器/worker | 下载成片(video/mp4) |

**`POST /jobs` 的素材字段**(前端从 `/assets` 选出的文件名,worker 据此下载):

| 字段 | 含义 |
|---|---|
| `driver` | 驱动视频素材文件名(worker 从 `/assets/<driver>` 下载;必须) |
| `voice` | `"云希"` / `"晓晓"`(→ edge-tts 通用音色)或 `"克隆"`(→ CosyVoice 声音克隆) |
| `voice_ref` | 声样素材文件名(`voice=="克隆"` 时用;空则回退驱动视频本人的声音) |
| `asset_imgs` | 商家图素材文件名数组(可空;非空走图文编排,空则纯口播) |

**lease 超时兜底**:running 任务超过 30 分钟(`KRVOICE_LEASE_TIMEOUT` 秒可调)没结果,
下次 `GET /jobs/next` 时自动退回 queued,防 worker 掉线卡死。

**环境变量**:
- `KRVOICE_DATA`(默认 `~/krvoice-data`)—— 任务/成片存放目录。
- `KRVOICE_ASSETS`(默认 `~/cicy-ai/assets`)—— 素材库目录(不存在则创建)。
- `KRVOICE_PORT`(默认 `8000`)。
- `KRVOICE_LEASE_TIMEOUT`(默认 `1800` 秒)。

## 部署

### 1. cloudshell 起队列中心

```bash
cd ~/projects/KrVoiceAI
pip install flask
python web-koubo/server.py          # 监听 0.0.0.0:8000,静态页 + 队列 API + 素材库 API
# 数据落地默认 ~/krvoice-data(jobs/*.json + pub/*.mp4);素材默认 ~/cicy-ai/assets
```

把 `krvoice.cicy-ai.com` 反代/隧道到本机 `:8000`(前端和队列因此同源)。
素材可网页上传(前端每个分类的「＋」),也可直接把文件丢进 `~/cicy-ai/assets`。

### 2. Colab 起拉取式 worker(**已去 Drive**)

Colab 里(已装好 produce.sh 全套 GPU 环境,**无需再挂 Drive**):

```bash
cd /content/KrVoiceAI       # 仓库路径
pip install requests
QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
```

`QUEUE_URL` 默认就是 `https://krvoice.cicy-ai.com`,可不设。worker 会:
按 job 里的 `driver`/`voice_ref`/`asset_imgs` 从 `GET /assets/<name>` 下载素材到临时目录;
声音选「云希/晓晓」走 edge-tts 通用音色,「克隆」走 CosyVoice(声样 = `voice_ref`,空则回退
驱动视频本人);出 `<临时目录>/final.mp4` 后回传,跑完清理临时目录。

> 也可让 `produce.sh` 出片后顺手拉起(设 `WORKER_AUTOSTART=1`,默认关);别再用旧 `worker.py`。

### 3. 端到端测

1. 浏览器开 `https://krvoice.cicy-ai.com` → 每个分类「＋」上传或选已有素材(形象=video,
   声音=预置音色或 audio 声样,商家图=可选 image 多选) + 写文案 → 点「生成视频」。
2. 前端 `POST /jobs`(带 `driver`/`voice`/`voice_ref`/`asset_imgs`),弹窗进度条随真实 `status` 走。
3. Colab worker `GET /jobs/next` 领到任务 → 下载素材 → 出片 → `POST /jobs/<id>/result`。
4. 前端轮询到 `done`,弹窗播放 `/pub/<id>.mp4` 并可下载。

无 Colab 时快速验素材库 + 队列(不出片):

```bash
curl -F file=@boss.mp4 localhost:8000/assets            # 上传素材 -> {name, kind}
curl localhost:8000/assets                              # 列出素材
curl -O localhost:8000/assets/boss.mp4                  # 下载素材(worker 用法)
curl -X POST localhost:8000/jobs -H 'Content-Type: application/json' \
  -d '{"text":"测试","voice":"云希","driver":"boss.mp4"}'
curl localhost:8000/jobs/next        # 模拟 worker 领任务(变 running,带素材文件名)
curl -F status=done -F file=@some.mp4 localhost:8000/jobs/<id>/result
curl localhost:8000/jobs/<id>        # 应为 done,带 url=/pub/<id>.mp4
```
