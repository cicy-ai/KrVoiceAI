#!/bin/bash
# 发布成片:本地 http server(8188) + cloudflare 隧道公网链接,写 /content/public_url.txt
# 用法: bash publish.sh <成片路径>
set +e
FINAL="${1:-/content/final.mp4}"
PUB=/content/pub
PORT=8188
[ -f "$FINAL" ] || { echo "[publish] 无成片 $FINAL"; exit 0; }

mkdir -p "$PUB"
cp -f "$FINAL" "$PUB/final.mp4"

# 重启 server / 隧道(幂等)
pkill -f "http.server $PORT" 2>/dev/null
pkill -f "cicy-cft" 2>/dev/null
pkill -f "trycloudflare\|cloudflared" 2>/dev/null
sleep 1
nohup python3 -m http.server $PORT --directory "$PUB" >/content/httpd.log 2>&1 &

rm -f /content/cft.log /content/public_url.txt
if command -v npx >/dev/null 2>&1; then
  nohup npx -y cicy-cft $PORT > /content/cft.log 2>&1 &
fi

URL=""
for i in $(seq 1 30); do
  sleep 3
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /content/cft.log 2>/dev/null | head -1)
  [ -n "$URL" ] && break
done

# npx 不行就下 cloudflared 兜底
if [ -z "$URL" ]; then
  echo "[publish] cicy-cft 没出链接,退 cloudflared"
  [ -f /content/cloudflared ] || wget -q -O /content/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x /content/cloudflared
  nohup /content/cloudflared tunnel --url http://localhost:$PORT > /content/cft.log 2>&1 &
  for i in $(seq 1 30); do
    sleep 3
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /content/cft.log 2>/dev/null | head -1)
    [ -n "$URL" ] && break
  done
fi

if [ -n "$URL" ]; then
  echo "$URL/final.mp4" > /content/public_url.txt
  echo "🌐 公网直链: $URL/final.mp4"
else
  echo "[publish] 隧道没起来,看 /content/cft.log"
  tail -5 /content/cft.log 2>/dev/null
fi
