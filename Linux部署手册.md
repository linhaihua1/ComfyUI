# ComfyUI Linux / 云服务器部署手册

适用版本：ComfyUI 0.46.0 + 本地启动器（`launcher_src/launcher.py` + `login_gate.py`）
测试环境：Ubuntu 22.04 / 24.04，NVIDIA 显卡，Python 3.11

---

## 1. 架构说明

```
浏览器 ──HTTPS──> Nginx / Caddy ──HTTP──> 登录门禁 127.0.0.1:8188 ──> ComfyUI 127.0.0.1:8288
```

- 登录门禁负责账号校验、Cookie 会话、HTTP 与 WebSocket 反向代理，ComfyUI 本体只监听本机，不直接暴露。
- 门禁默认只监听 `127.0.0.1`；云服务器可传 `--host 0.0.0.0` 直接对外，但**强烈建议**前面加 Nginx/Caddy 提供 HTTPS。
- 启动器会自动检测显存并追加 `--vram-headroom`、`--fp8_e4m3fn-text-enc` 等参数，禁用云端合作节点，并启用 ComfyUI-Manager。

---

## 2. 服务器要求

| 项目 | 最低 | 推荐 |
| --- | --- | --- |
| 系统 | Ubuntu 22.04 x86_64 | Ubuntu 24.04 |
| CPU | 4 核 | 8 核 |
| 内存 | 16GB | 32GB |
| 显卡 | NVIDIA 6GB 显存（RTX 2060 可跑 SDXL 1024） | 12GB 以上 |
| 驱动 | 支持 CUDA 12.8（驱动 ≥ 570） | 最新版 |
| 磁盘 | 30GB 可用 | 100GB（模型较多时） |
| Python | 3.11 | 3.11 |

云厂商需选 **GPU 实例**（如阿里云 gn7i、腾讯云 GN7），并确认已安装 NVIDIA 驱动：

```bash
nvidia-smi
```

---

## 3. 安装系统依赖

```bash
sudo apt update
sudo apt install -y git curl wget ffmpeg libgl1 libglib2.0-0 \
    python3.11 python3.11-venv python3.11-dev build-essential
```

若系统源没有 Python 3.11（如 Ubuntu 22.04 默认 3.10）：

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

---

## 4. 获取代码

```bash
sudo mkdir -p /opt && sudo chown $USER:$USER /opt
git clone https://github.com/linhaihua1/ComfyUI.git /opt/ComfyUI
cd /opt/ComfyUI
```

仓库已包含启动器源码（`launcher_src/`）、`comfyui_launcher.ps1`、使用说明和三个工作流；Linux 使用 `launcher_src/launcher.py`，不需要 ps1/exe。

---

## 5. 创建环境并安装依赖

### 5.1 虚拟环境与 PyTorch

```bash
cd /opt/ComfyUI
python3.11 -m venv venv
source venv/bin/activate
python -m pip install -U pip wheel

# 驱动 ≥ 570（CUDA 12.8+）：
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
# 国内网络慢可改用清华镜像：
# pip install torch torchvision torchaudio --index-url https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu128
```

驱动较旧（< 570）时把 `cu128` 换成 `cu121`。

### 5.2 ComfyUI 依赖与节点管理器

```bash
pip install -r requirements.txt
pip install -U --pre comfyui-manager
```

### 5.3 校验

```bash
python -c "import torch;print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

输出应类似 `2.11.0+cu128 True NVIDIA GeForce RTX 4090`。

---

## 6. 下载模型

目录约定：`models/checkpoints/`。

SDXL Base 1.0（约 6.9GB）：

```bash
pip install -U modelscope
modelscope download --model AI-ModelScope/stable-diffusion-xl-base-1.0 \
    --include 'sd_xl_base_1.0.safetensors' --local_dir models/checkpoints
```

SD1.5 fp16（约 2GB）：

```bash
modelscope download --model AI-ModelScope/stable-diffusion-v1-5 \
    --include 'v1-5-pruned-emaonly-fp16.safetensors' --local_dir models/checkpoints
```

也可用 hf-mirror + aria2：

```bash
export HF_ENDPOINT=https://hf-mirror.com
aria2c -x 8 -s 8 -d models/checkpoints \
  https://hf-mirror.com/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors
```

校验（SDXL 官方哈希）：

```bash
sha256sum models/checkpoints/sd_xl_base_1.0.safetensors
# 31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b
```

---

## 7. 启动

### 7.1 手动启动（调试用）

```bash
cd /opt/ComfyUI
export COMFY_ADMIN_USER=admin
export COMFY_ADMIN_PASS='换成你的强密码'
venv/bin/python launcher_src/launcher.py --host 0.0.0.0 --port 8188 --no-browser
```

浏览器访问 `http://服务器IP:8188`，使用上面的账号登录。

> `--no-browser` 在无桌面服务器上可省略（非本机监听时启动器会自动跳过打开浏览器）。
> 端口被占用时启动器会自动顺延为 8189、8190…（内部端口为对外端口 +100）。
> 以 `launcher_src/launcher.py` 脚本方式启动时，启动器会自动识别上级 ComfyUI 根目录，在仓库根目录执行上面的命令即可。

### 7.2 systemd 常驻（推荐）

创建 `/etc/systemd/system/comfyui.service`：

```ini
[Unit]
Description=ComfyUI with login gate
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=comfy
Group=comfy
WorkingDirectory=/opt/ComfyUI
Environment=COMFY_ADMIN_USER=admin
Environment=COMFY_ADMIN_PASS=换成你的强密码
ExecStart=/opt/ComfyUI/venv/bin/python /opt/ComfyUI/launcher_src/launcher.py --host 127.0.0.1 --port 8188 --no-browser
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -m -d /opt/ComfyUI comfy 2>/dev/null || true
sudo chown -R comfy:comfy /opt/ComfyUI
sudo systemctl daemon-reload
sudo systemctl enable --now comfyui
sudo systemctl status comfyui --no-pager
journalctl -u comfyui -f
```

配合 Nginx 时 `--host` 用 `127.0.0.1`；仅内网测试可直接 `0.0.0.0` 并跳过第 8 章。

---

## 8. HTTPS 反向代理（公网必做）

### 8.1 Nginx

```bash
sudo apt install -y nginx
```

`/etc/nginx/conf.d/comfyui.conf`：

```nginx
server {
    listen 80;
    server_name comfy.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name comfy.example.com;

    ssl_certificate     /etc/letsencrypt/live/comfy.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/comfy.example.com/privkey.pem;

    client_max_body_size 2048m;   # 上传大图/模型
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:8188;
        proxy_http_version 1.1;
        proxy_set_header Host $host;                    # 必须：ComfyUI 会校验 Host/Origin
        proxy_set_header X-Forwarded-Proto $scheme;     # 门禁据此设置 Secure Cookie
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;         # WebSocket
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

```bash
sudo certbot --nginx -d comfy.example.com
sudo nginx -t && sudo systemctl reload nginx
```

### 8.2 Caddy（更简单，自动证书）

```caddyfile
comfy.example.com {
    reverse_proxy 127.0.0.1:8188 {
        header_up Host {host}
    }
    request_body {
        max_size 2GB
    }
}
```

Caddy 会自动传递 `X-Forwarded-Proto` 与 WebSocket。

### 8.3 防火墙与安全组

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80,443/tcp
sudo ufw enable
```

云控制台安全组只放行 22/80/443；**不要**放行 8288，8188 也不应对公网开放（Nginx 方案下它只监听 127.0.0.1）。

负载均衡/云监控的健康检查请探测 `GET /login`（返回 200），不要探测需要登录的接口（如 `/system_stats`，未登录返回 401）。

---

## 9. 登录与安全说明

| 项目 | 说明 |
| --- | --- |
| 账号来源 | 环境变量 `COMFY_ADMIN_USER` / `COMFY_ADMIN_PASS`，默认 `admin / admin@123` |
| 会话 | HttpOnly Cookie `comfy_admin`，7 天有效，登出 `/logout` |
| 防爆破 | 同一 IP 10 分钟内失败 5 次，锁定 5 分钟（返回 429） |
| HTTPS | 经 `X-Forwarded-Proto: https` 时 Cookie 自动加 `Secure` |
| 未登录访问 | 页面请求 302 跳登录页；接口与 WebSocket 返回 401 JSON |
| 后端端口 | ComfyUI 只监听 127.0.0.1:8288，无法绕过门禁直连 |

> 默认密码只适合本机测试，云服务器部署前必须修改，并确认 8288 未对外暴露。

---

## 10. 验证清单

```bash
# 1. 端口：8188 在监听，8288 只在本机
ss -lntp | grep -E '8188|8288'

# 2. 未登录：接口应 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8188/system_stats

# 3. 登录页 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8188/login

# 4. 登录并访问
curl -s -c /tmp/c.txt -o /dev/null -w '%{http_code}\n' \
  -d "username=admin&password=你的密码" http://127.0.0.1:8188/login
curl -s -b /tmp/c.txt -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8188/system_stats

# 5. Manager 版本
curl -s -b /tmp/c.txt http://127.0.0.1:8188/v2/manager/version
```

浏览器侧：登录后能看到画布、左侧工作流中有「本地SD1.5文生图」「本地SDXL-Seedream5.5文生图」，菜单里有「管理扩展功能」。

本机（Windows）已验证：`--host 0.0.0.0` 监听、未登录 401/登录后 200、5 次失败锁定 429、HTTPS 转发时 Cookie 带 `Secure`。Linux 复用同一套 Python 代码，部署后请按上面清单复测。

---

## 11. 常见问题

1. **页面 403 Forbidden**：Nginx 未透传 `Host`。确认 `proxy_set_header Host $host;`，且 `X-Forwarded-Proto` 与访问协议一致。
2. **HTTPS 下登录后仍跳登录页**：Cookie 带了 `Secure` 但实际是 HTTP 访问。统一走 HTTPS，或去掉反代中的 `X-Forwarded-Proto`。
3. **登录提示“尝试次数过多”**：触发防爆破锁定，等待 5 分钟或重启服务。
4. **上传图片 413**：Nginx `client_max_body_size` 调大（建议 2048m）。
5. **WebSocket 断线/进度不动**：反代需 `Upgrade`/`Connection` 头，`proxy_read_timeout` 调大（3600s）。
6. **显存不足 OOM**：启动器已自动按显存档位设 `--vram-headroom`；仍不足时把工作流分辨率降到 768，或手动追加 `--reserve-vram 1.0`。
7. **`nvidia-smi` 正常但 CUDA 不可用**：torch 与驱动版本不匹配，按第 5.1 节换 `cu121`/`cu128` 重装。
8. **Manager 下载节点很慢或失败**：国内服务器可在 Manager 设置里配置代理或镜像；也可手动 `git clone` 节点到 `custom_nodes/` 后重启。
9. **端口被占用**：启动器自动顺延到 8189+，以启动日志中的“访问地址”为准；systemd 方案请固定 `--port`。
10. **服务重启后模型要重新加载**：属正常现象，首次出图会慢 30~60 秒。

---

## 12. 更新与备份

```bash
# 更新代码（保留本地提交）
cd /opt/ComfyUI
git pull --rebase

# 更新依赖
venv/bin/pip install -r requirements.txt

# 重启
sudo systemctl restart comfyui
```

需要备份的内容：

- `models/`（模型，体积大，可不纳入代码备份）
- `user/default/workflows/`（工作流）
- `custom_nodes/`（手动安装的节点）
- `output/`（生成结果）

---

## 13. 参数速查

| 参数 | 说明 |
| --- | --- |
| `--host 0.0.0.0` | 门禁监听所有网卡（默认 127.0.0.1） |
| `--port 8188` | 对外端口，占用时自动顺延 |
| `--no-browser` | 不自动打开浏览器（云服务器自动生效） |
| `--install-env` | 自动创建 venv 并安装 torch/依赖（Linux 可用） |
| `--disable-partner-nodes` | 禁用云端合作节点（启动器默认追加） |
| `--enable-manager` | 启用 ComfyUI-Manager（启动器默认追加） |
| `COMFY_ADMIN_USER/PASS` | 覆盖登录账号密码 |
| `COMFY_NO_FP8_TE=1` | 关闭文本编码器 fp8 低显存优化 |
