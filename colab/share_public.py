#!/usr/bin/env python3
"""成片拷回 Drive outputs/ 并设为「任何有链接的人可看」,打印公开链接。
依赖 Colab 里用户已 auth.authenticate_user()(凭据是运行时级别,脚本里直接可用)。
用法: python share_public.py /content/final.mp4 [通知邮箱]
"""
import sys, os, shutil, time

src = sys.argv[1] if len(sys.argv) > 1 else "/content/final.mp4"
notify = sys.argv[2] if len(sys.argv) > 2 else ""
if not os.path.exists(src):
    print("[share] 找不到成片:", src); sys.exit(0)
if not os.path.isdir("/content/drive/MyDrive"):
    print("[share] Drive 未挂载,跳过"); sys.exit(0)

out_dir = "/content/drive/MyDrive/latentsync/outputs"
os.makedirs(out_dir, exist_ok=True)
dst = f"{out_dir}/final_{time.strftime('%m%d_%H%M%S')}.mp4"
shutil.copy(src, dst)
name = os.path.basename(dst)
print("[share] 已拷贝到 Drive:", dst)

try:
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default()
    svc = build("drive", "v3", credentials=creds)
except Exception as e:
    print("[share] 无 Google 凭据(在 notebook 跑一次 auth.authenticate_user() 即可),跳过公开:", e)
    sys.exit(0)

fid = None
for _ in range(20):                      # 等 Drive 挂载同步出文件 id
    r = svc.files().list(q=f"name='{name}' and trashed=false", fields="files(id)").execute().get("files", [])
    if r: fid = r[0]["id"]; break
    time.sleep(3)
if not fid:
    print("[share] Drive 里没找到文件(同步慢?),稍后手动分享"); sys.exit(0)

svc.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
drive_url = f"https://drive.google.com/file/d/{fid}/view"
print("🔗 公开链接(任何人可看):", drive_url)
if notify:
    # 邮件正文里带上所有链接(公网直链 + Drive 公开链接),QQ 邮箱直接点
    tunnel = ""
    if os.path.exists("/content/public_url.txt"):
        tunnel = open("/content/public_url.txt").read().strip()
    msg = "你的口播数字人成片:\n"
    if tunnel: msg += f"公网直链(推荐,点开即播): {tunnel}\n"
    msg += f"Drive 链接(已设公开): {drive_url}"
    try:
        svc.permissions().create(fileId=fid, body={"type": "user", "role": "reader", "emailAddress": notify},
                                 sendNotificationEmail=True, emailMessage=msg).execute()
        print("[share] 已邮件通知:", notify)
    except Exception as e:
        print("[share] 邮件通知失败(链接反正是公开的):", e)
