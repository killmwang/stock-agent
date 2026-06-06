# 腾讯云 CVM 部署笔记

本文档用于课堂展示版部署。目标是让浏览器可以直接访问页面，不需要观众在本地启动前后端。

## 服务器建议

最低可用配置：

- Ubuntu 22.04
- 2 核 CPU
- 2GB 内存
- 50GB 系统盘
- 3Mbps 带宽

2GB 内存偏紧，建议开启 2GB swap。

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

## 安装基础依赖

```bash
sudo apt update
sudo apt install -y git curl wget unzip nginx python3 python3-venv python3-pip
sudo apt remove -y nodejs npm
sudo apt autoremove -y
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v
npm -v
python3 --version
nginx -v
```

## 拉取代码

```bash
cd /home/ubuntu
git clone https://github.com/你的用户名/你的仓库名.git
cd 你的仓库名
```

## 配置后端环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r web-app/backend/requirements.txt
cp .env.example .env
nano .env
```

`.env` 至少填写：

```env
DEEPSEEK_API_KEY=你的 DeepSeek API Key
OPENAI_API_KEY=你的 DeepSeek API Key
OPENAI_BASE_URL=https://api.deepseek.com
DEEPSEEK_BASE_URL=https://api.deepseek.com
JWT_SECRET=换成随机字符串
```

## 启动后端

课堂临时演示可以先用前台运行：

```bash
cd /home/ubuntu/你的仓库名/web-app/backend
source /home/ubuntu/你的仓库名/.venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

稳定演示建议后续改成 systemd 服务。

## 构建前端

另开终端：

```bash
cd /home/ubuntu/你的仓库名/web-app/frontend
npm install
npm run build
```

构建产物通常在：

```text
web-app/frontend/dist
```

## 配置 Nginx

创建配置：

```bash
sudo nano /etc/nginx/sites-available/stock-agent
```

写入：

```nginx
server {
    listen 80;
    server_name _;

    root /home/ubuntu/你的仓库名/web-app/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/stock-agent /etc/nginx/sites-enabled/stock-agent
sudo nginx -t
sudo systemctl reload nginx
```

然后访问：

```text
http://你的公网 IP
```

## 安全提醒

- 不要把 `.env` 上传到 GitHub。
- 腾讯云安全组需要放行 80 端口。
- DeepSeek API Key 只放在服务器后端环境变量里，不要放进前端代码。
- 课堂演示访问码可以使用简单值，正式公开访问时要更换。
