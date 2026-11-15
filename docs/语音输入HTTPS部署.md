# 语音输入 HTTPS 部署指南

> 智能助手页（`/ask`）的 🎙 语音输入基于 Web Speech API，**只在安全上下文（HTTPS 或 localhost）下开放**。
> 本文档回答两件事：为什么公网 HTTP 部署下语音是灰的，以及三条上 HTTPS / 本地可用的路径。
> 命令中的服务器地址一律用 `<DOMAIN>` / `<SERVER_IP>` 占位，替换成你自己的再执行。

---

## 一、为什么语音要 HTTPS

- Web Speech API（`SpeechRecognition`）与 `getUserMedia` 一样，浏览器**只在安全上下文**开放：`https://` 页面，或 `http://localhost` / `http://127.0.0.1`。普通 HTTP 公网页面里 `window.isSecureContext === false`，浏览器直接不提供语音能力。
- 本项目的应用壳是**纯标准库 `http.server`（不支持 TLS）**，公网默认部署就是 HTTP。所以前端在 `ask.html` 里做了探测降级：
  - 浏览器没有该 API → 按钮直接隐藏；
  - 有 API 但非安全上下文（当前 HTTP 部署的情况）→ 按钮置灰，点击弹 toast 说明；
  - **其余功能完全不受影响**——问答、录单、看板在 HTTP 下一切照常，语音是唯一被浏览器门槛挡住的功能。

自检方法（浏览器控制台，在 `/ask` 页面按 F12）：

```js
window.isSecureContext                                            // true = 安全上下文，语音可开
window.SpeechRecognition || window.webkitSpeechRecognition        // 非 undefined = 浏览器有该 API
```

---

## 二、路径 A：域名 + Caddy / nginx + certbot（推荐）

适合：有域名、长期部署。走正规 Let's Encrypt 证书，自动续期，浏览器零警告。

前置：把 `<DOMAIN>` 的 DNS A 记录指到服务器公网 IP，防火墙放行 **80 和 443**（80 用于证书签发挑战和 HTTP 跳转）。

### 方案 1：Caddy（最省事，单文件自动签）

```bash
# Debian/Ubuntu 安装（官方 apt 源，自带 systemd 服务；其他平台见 caddy 官网下载单文件即可）
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

编辑 `/etc/caddy/Caddyfile`，全文件只需这几行（首次启动自动签 Let's Encrypt 证书并续期，无需 certbot）：

```caddyfile
<DOMAIN> {
    reverse_proxy 127.0.0.1:8090
}
```

```bash
sudo systemctl reload caddy
```

- Caddy 对 `text/event-stream` 默认即时刷新，`/api/ask/stream` 的 SSE 流**不需要额外配置**。
- 验证：`curl -sI https://<DOMAIN>/ | head -3` 返回 200，浏览器访问页面看 🎙 是否点亮。

### 方案 2：nginx + certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx

# 先起一个 80 端口的反代（certbot 会在此基础上自动改造成 443 + HTTP 跳转）
sudo tee /etc/nginx/sites-available/t2s >/dev/null <<'EOF'
server {
    listen 80;
    server_name <DOMAIN>;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/t2s /etc/nginx/sites-enabled/t2s
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 签发证书并自动改写配置（含自动续期定时任务）
sudo certbot --nginx -d <DOMAIN>
```

**坑位标注（SSE 必须关缓冲）**：`/api/ask/stream` 是 SSE（`text/event-stream`）。nginx 默认开启 `proxy_buffering`，会把上游响应攒进缓冲区再下发，前端表现为「一直在思考…」直到回答全部生成完才一次性出字——流式体验完全丢失。certbot 改写配置后，**确认 443 的 server 块里流式接口的 location 有 `proxy_buffering off`**，最终形态参考：

```nginx
server {
    listen 443 ssl;
    server_name <DOMAIN>;
    # ssl_certificate / ssl_certificate_key 两行由 certbot 自动写入并续期，不用手抄

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    # SSE 专用：关缓冲才能逐事件推送
    location /api/ask/stream {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

**配套建议**：上了反代后，把 Talk2Stock 的监听收到本机回环（环境变量 `HOST=127.0.0.1`，见 README 环境变量表），公网只能走 TLS 入口，不能绕过证书直连 8090。

---

## 三、路径 B：自签证书（无域名，仅演示用）

适合：临时演示、没有域名。自签证书能让 `window.isSecureContext === true`，**语音 API 可用**；但浏览器地址栏会挂「不安全」警告，首次访问要手动点「高级 → 继续前往」，演示观感差——**正式演示建议走路径 C**。

```bash
# 一行签出带 IP SAN 的自签证书（浏览器校验证书时要求主机名在 SAN 里）
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
  -keyout /etc/nginx/ssl/t2s.key -out /etc/nginx/ssl/t2s.crt \
  -subj "/CN=talk2stock-demo" \
  -addext "subjectAltName=IP:<SERVER_IP>"
```

nginx 配置（把路径 A 的 server 块换成 ssl 监听即可，location 部分**原样保留 SSE 的 `proxy_buffering off`**）：

```nginx
server {
    listen 443 ssl;
    server_name <SERVER_IP>;
    ssl_certificate     /etc/nginx/ssl/t2s.crt;
    ssl_certificate_key /etc/nginx/ssl/t2s.key;

    # location 块照抄路径 A（含 /api/ask/stream 的关缓冲段）
}
```

浏览器首次访问 `https://<SERVER_IP>/` 会出现证书警告，点「高级 → 继续前往」进入；此后语音 🎙 即点亮。

**中间方案备注**：Let's Encrypt 自 2025 年起支持给**公网 IP 地址**直接签证书（short-lived 证书，有效期约 6 天，需要较新的 acme 客户端配合自动续期）。有固定公网 IP 时，可作为「没有域名」与「自签观感差」之间的折中，具体以 Let's Encrypt 官方文档当前状态为准。

---

## 四、路径 C：本机 localhost 演示（零改动，语音直接可用）

`localhost` / `127.0.0.1` 本身就是安全上下文——**不需要任何证书**。

在演示机上直接：

```bash
python3 app/server.py
# 浏览器访问 http://localhost:8090
```

语音 🎙 直接可用，适合课堂/店内演示。

**坑位标注**：必须用 `localhost` 这个主机名访问。换成 `http://<SERVER_IP>:8090`（哪怕只是局域网 IP）就不是安全上下文，语音照样置灰。

---

## 五、注意事项

1. **先开鉴权再上公网**：`/api/*` 默认不鉴权（方便本地开发）。把 8090 暴露到公网（无论 HTTP 还是 HTTPS）之前，**必须**设 `T2S_REQUIRE_AUTH=1` 并改掉默认 `T2S_DEMO_PASS`，细节以 README「安全说明」为准。
2. **麦克风权限是浏览器按站点授予的**：HTTPS 迁移（或 HTTP → HTTPS 切换域名身份）后，第一次点 🎙 会弹权限请求，点一次「允许」即可；如果之前拒绝过，到浏览器站点设置里重置麦克风权限再试。
3. **语音识别的网络依赖**：Chrome 的 SpeechRecognition 在部分环境走 Google 服务，服务器/客户端网络不通时会报网络错误（前端已 toast 提示）；iframe 内嵌 WebView 可能整体不提供该 API（按钮自动隐藏）。这两类都是客户端环境问题，与本服务端部署无关。
4. **服务端零改动**：以上三条路径都是在前端入口处终止 TLS，`app/server.py` 保持纯标准库 HTTP 不动。
