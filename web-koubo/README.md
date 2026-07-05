# 口播工坊 · 中心队列架构

前端和队列**同源**(都在 `krvoice.cicy-ai.com`),Colab 当拉取式 GPU worker 主动拉任务。
前端完全不知道 Colab 在哪,无需 CORS、无需配任何外部地址。

**素材统一走阿里云 OSS 直传(已彻底去掉 Google Drive 和 cloudshell 本地素材)**:
用户在浏览器里把驱动视频 / 声样 / 商家图**直接 PUT 到 OSS**(国内秒传,不经国外 cloudshell)。
队列中心只负责发预签名 URL、列对象、删对象;worker 从 OSS **公读 URL** 匿名 HTTP GET 下载。

```
口播工坊前端(krvoice.cicy-ai.com,同源)
  --GET /oss/presign 拿预签名 PUT URL--> 队列中心 server.py(cloudshell,:8000)
  ==浏览器 XHR PUT 文件(带进度条)============> 阿里云 OSS(krvoice-assets,公读桶)
  --GET /assets 列素材 / DELETE /assets?key= 删--> 队列中心(用 oss2 代查/代删)
  --POST /jobs 提交(带素材 OSS key)------------> 队列中心
Colab worker pull_worker.py(GPU)
  --GET /jobs/next 拉--> 从 OSS 公读 URL 下载素材 --produce.sh 出片--> POST /jobs/<id>/result 回传
前端 --GET /jobs/<id> 轮询--> done 后播 /pub/<id>.mp4
```

## 三个文件

| 文件 | 跑在哪 | 干什么 |
|------|--------|--------|
| `web-koubo/server.py` | cloudshell(无 GPU) | serve 静态页 + 任务队列 API + **OSS 素材 API**(预签名 / 列 / 删,用 `oss2`),**不出片**、**不存素材**。任务/成片存 `~/krvoice-data`;素材全在 OSS。 |
| `colab/pull_worker.py` | Colab(有 GPU) | 循环 `GET /jobs/next` 拉任务,按 job 里的 OSS key 从**公读 URL** 下载素材到临时目录(用 `requests`,不需要 `oss2`/AK),调 `produce.sh` 出片,`POST /jobs/<id>/result` 回传。**不再挂 Drive、不再打 `/assets`。** |
| `web-koubo/index.html` | 浏览器(同源) | 口播工坊前端。素材**直传 OSS**(预签名 PUT + XHR 进度条),job 提交 OSS key,轮询 `/jobs/<id>`,`done` 播 `/pub/<id>.mp4`。 |

> 旧的 `colab/worker.py`(队列+worker 合一)已被上面两者取代,保留仅作参考。

## 阿里云 OSS 配置

- bucket:`krvoice-assets`(公读桶,CORS 已开 `*`,允许浏览器直传)
- endpoint:`https://oss-cn-hangzhou.aliyuncs.com`(杭州)
- 公读下载基址:`https://krvoice-assets.oss-cn-hangzhou.aliyuncs.com/<key>`
- 对象 key 统一:`assets/<时间戳_uuid_安全化原名>`
- **AK 绝不硬编码进代码**,全走环境变量:

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `OSS_KEY_ID` | (无) | 访问密钥 ID,**部署时注入** |
| `OSS_KEY_SECRET` | (无) | 访问密钥 Secret,**部署时注入** |
| `OSS_ENDPOINT` | `https://oss-cn-hangzhou.aliyuncs.com` | OSS endpoint |
| `OSS_BUCKET` | `krvoice-assets` | bucket 名 |

`OSS_KEY_ID` / `OSS_KEY_SECRET` **缺失时** `/oss/presign`、`/assets`(GET/DELETE)一律返回
`500 {"error":"OSS 未配置:缺 OSS_KEY_ID / OSS_KEY_SECRET 环境变量"}`,服务不崩、其余 API 正常。

server 端用 `oss2` 库(签名 / 列 / 删):`pip install oss2 flask`。
worker 端只用 `requests` 匿名 GET 公读 URL,**不需要 `oss2`、不需要 AK**;公读基址可用
`OSS_PUBLIC_BASE` 覆盖(默认 `https://krvoice-assets.oss-cn-hangzhou.aliyuncs.com`)。

## API 契约(server.py)

所有响应带 `Access-Control-Allow-Origin: *`(同源其实用不上,方便调试)。

| 方法 · 路径 | 谁调 | body / 返回 |
|---|---|---|
| `GET /` | 浏览器 | 返回 `index.html` |
| `GET /<静态文件>` | 浏览器 | web-koubo/ 同目录静态资源 |
| `GET /oss/presign?filename=<原名>&kind=<video\|audio\|image>` | 前端上传 | → `200 {key, put_url, get_url}`。`put_url`=预签名 PUT(有效 1 小时),前端 `fetch(put_url,{method:'PUT',body:file})` 直传;`get_url`=公读下载 URL;`key`=OSS 对象 key。OSS 未配置 → `500` |
| `GET /assets` | 前端 | → `200 [{key, name(去 assets/ 前缀), kind(video\|audio\|image\|other), size, url(公读)}, ...]` 列 OSS `assets/` 前缀对象。未配置 → `500` |
| `DELETE /assets?key=<key>` | 前端 | → `200 {ok:true}` 删除一个 OSS 对象;key 非法(非 `assets/` 前缀)→ `400`;未配置 → `500` |
| `POST /jobs` | 前端提交 | body(JSON)`{text, tpl?, voice?, driver?, voice_ref?, asset_imgs?, avatar?, desc?}` → `201 {id, status:"queued", check}`;`text` 必填,缺 → `400`。素材字段见下表 |
| `GET /jobs/<id>` | 前端轮询 | → `200 {id, status:queued\|running\|done\|failed, progress?, url?, error?, log_tail, ...}`;无 → `404`。`log_tail`=实时日志最后 ~1KB,快速瞥一眼(完整日志走 `/jobs/<id>/log`);旧 `log` 字段不下发 |
| `GET /jobs/next` | worker 拉 | → `200 <job JSON>`(取最早 queued,标记 running + 记 lease);无 → `204` |
| `POST /jobs/<id>/status` | worker 汇报(可选) | body(JSON)`{status?, progress?}` → `200 {ok:true}`;无 job → `404` |
| `POST /jobs/<id>/log` | worker 实时回传日志 | body(JSON)`{chunk:"<这批新日志行>"}` → `200 {ok:true}`;无 job → `404`。累积到 `logs/<id>.log`,只保留尾部 ~20KB(`KRVOICE_LOG_CAP` 字节可调) |
| `GET /jobs/<id>/log` | 前端/curl 实时看 | → `200 <text/plain>` 该任务累积的实时日志纯文本;无 job → `404`。**任何人 curl 就能实时看到 worker 干到哪** |
| `POST /jobs/<id>/result` | worker 回传 | multipart:`status=done\|failed`,done 带 `file=<mp4>`,failed 带 `error=<原因>` → `200 {ok:true, url?}`。done 时成片存 `pub/<id>.mp4`,`job.url="/pub/<id>.mp4"` |
| `GET /pub/<file>` | 浏览器/worker | 下载成片(video/mp4) |

> 旧的 `POST /assets`(multipart 上传)和 `GET /assets/<file>`(下载)已删除:上传改为浏览器直传 OSS,下载改为 worker 打公读 URL。

**`POST /jobs` 的素材字段**(前端从 `/assets` 选出的 **OSS key**,worker 据此拼公读 URL 下载):

| 字段 | 含义 |
|---|---|
| `driver` | 驱动视频 OSS key(必须;worker 也兼容直接给公读 http(s) url) |
| `voice` | `"云希"` / `"晓晓"`(→ edge-tts 通用音色)或 `"克隆"`(→ CosyVoice 声音克隆) |
| `voice_ref` | 声样 OSS key(`voice=="克隆"` 时用;空则回退驱动视频本人的声音) |
| `asset_imgs` | 商家图 OSS key 数组(可空;非空走图文编排,空则纯口播) |

**job 契约(最终)**:
`{ text(必填), tpl, voice("云希"|"晓晓"|"克隆"), driver(视频 OSS key), voice_ref(音频 OSS key,克隆用), asset_imgs([图片 OSS key 数组]) }`

**lease 超时兜底**:running 任务超过 30 分钟(`KRVOICE_LEASE_TIMEOUT` 秒可调)没结果,
下次 `GET /jobs/next` 时自动退回 queued,防 worker 掉线卡死。

**其他环境变量**:
- `KRVOICE_DATA`(默认 `~/krvoice-data`)—— 任务/成片/**实时日志(`logs/<id>.log`)** 存放目录。
- `KRVOICE_PORT`(默认 `8000`)。
- `KRVOICE_LEASE_TIMEOUT`(默认 `1800` 秒)。
- `KRVOICE_LOG_CAP`(默认 `20480` 字节)—— 单任务实时日志只保留尾部这么多,防无限膨胀。

## worker 实时日志(不再是黑盒)

worker 从前用 `subprocess.run(capture_output=True)` 把 `produce.sh` 输出全吞进内存,失败只在
`result` 里给一段截断日志——是个黑盒。现在改成 **`subprocess.Popen` + 逐行读**,边跑边把日志实时
回传队列中心,任何人 curl 就能看到 worker 干到哪、卡在哪、报什么错。

**日志怎么流转**:

```
produce.sh(逐阶段 echo:抽参考音/克隆/LatentSync/编排)
  │  worker 用 Popen 合并 stdout+stderr 逐行读
  ├─ 追加到本地 /content/pull_worker.log(Colab 端直接看)
  └─ 攒够 ~10 行或每 ~2.5s → POST /jobs/<id>/log {chunk}  ──▶  队列中心累积到 logs/<id>.log(尾部 20KB)
                                                                      │
   前端进度弹窗轮询 GET /jobs/<id>/log ◀────── 任何人 curl GET /jobs/<id>/log ◀┘
   (等宽字体滚动显示,失败标红)              (纯文本,实时)
```

worker 还在关键阶段打醒目标记行并更新 `progress`:领任务 → `▶ 下载素材`(10)→ `✔ 素材下载完成`
→ `▶ 开始出片`(30)→ `✔ 出片成功`(95)→ `▶ 回传成片`;空闲时每约 1 分钟打一次心跳,证明它还活着。
失败/超时/异常都会打醒目标记行。**详细过程日志已实时传过,不再依赖结束时那一坨。**

**curl 实时看某任务日志**:

```bash
# 一次性看当前累积日志
curl https://krvoice.cicy-ai.com/jobs/<id>/log

# 每 2 秒刷新,像 tail -f 一样盯着 worker 干活
watch -n2 curl -s https://krvoice.cicy-ai.com/jobs/<id>/log
```

## 前端上传按钮(直传 OSS,带进度条)

每个素材分类(形象=视频 / 声音=音频 / 商家图=图片)的「＋上传」按钮:

1. 点击弹文件选择(video/audio/image 各自 `accept` 过滤;商家图支持多选)。
2. `GET /oss/presign?filename=&kind=` 拿 `put_url`。
3. **`XMLHttpRequest` PUT 直传 OSS**,`upload.onprogress` 实时刷新**右下角进度卡片**:百分比 + 上传速度(fetch 不支持上传进度,所以用 xhr),不再"卡住不知道死活"。
4. 传完自动 `GET /assets` 刷新该分类列表,新素材直接选中。
5. 失败给明确红色提示 + 「重试」按钮(重新走预签名再传)。

素材列表项:视频/音频给图标 + 文件名,图片给**公读 URL 缩略图**;可点选(高亮 ✓),悬停出现「×」可删除(`DELETE /assets?key=`)。

## 部署

### 1. cloudshell 起队列中心(需注入 OSS AK)

```bash
cd ~/projects/KrVoiceAI
pip install flask oss2
export OSS_KEY_ID=<你的AK>          # 必注入,别写进代码/仓库
export OSS_KEY_SECRET=<你的SK>
# OSS_ENDPOINT / OSS_BUCKET 有默认值(杭州 / krvoice-assets),可不设
python web-koubo/server.py          # 监听 0.0.0.0:8000,静态页 + 队列 API + OSS 素材 API
```

把 `krvoice.cicy-ai.com` 反代/隧道到本机 `:8000`(前端和队列因此同源)。
素材由浏览器直传 OSS,cloudshell 本地**不再存任何素材**。

### 2. Colab 起拉取式 worker(**不需要 OSS AK**)

```bash
cd /content/KrVoiceAI
pip install requests
QUEUE_URL=https://krvoice.cicy-ai.com python colab/pull_worker.py
# 公读基址默认 https://krvoice-assets.oss-cn-hangzhou.aliyuncs.com,可用 OSS_PUBLIC_BASE 覆盖
```

worker 按 job 里的 `driver`/`voice_ref`/`asset_imgs`(OSS key)拼公读 URL 匿名下载到临时目录;
声音「云希/晓晓」走 edge-tts,「克隆」走 CosyVoice(声样=`voice_ref`,空则回退驱动视频本人);
出 `<临时目录>/final.mp4` 后回传,跑完清理。

### 3. 端到端测

1. 浏览器开 `https://krvoice.cicy-ai.com` → 每个分类「＋」上传(看右下角进度条直传 OSS)或选已有素材 + 写文案 → 点「生成视频」。
2. 前端 `POST /jobs`(带 `driver`/`voice`/`voice_ref`/`asset_imgs` 的 OSS key),弹窗进度随真实 `status` 走。
3. Colab worker `GET /jobs/next` 领到任务 → 从 OSS 公读 URL 下载素材 → 出片 → `POST /jobs/<id>/result`。
4. 前端轮询到 `done`,弹窗播放 `/pub/<id>.mp4` 并可下载。

无 Colab 时快速验 OSS 素材 + 队列(不出片,需已注入 OSS AK):

```bash
# 1) 拿预签名并直传一个文件到 OSS
SIG=$(curl -s 'localhost:8000/oss/presign?filename=boss.mp4&kind=video')
PUT=$(echo "$SIG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["put_url"])')
KEY=$(echo "$SIG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
curl -s -X PUT -T boss.mp4 "$PUT"                       # 直传 OSS
curl -s localhost:8000/assets                           # 列出(应看到刚传的 key)
# 2) 用 key 提交 job,模拟 worker 领取
curl -s -X POST localhost:8000/jobs -H 'Content-Type: application/json' \
  -d "{\"text\":\"测试\",\"voice\":\"云希\",\"driver\":\"$KEY\"}"
curl -s localhost:8000/jobs/next                        # 变 running,带 OSS key
# 3) 删素材
curl -s -X DELETE "localhost:8000/assets?key=$KEY"
```
