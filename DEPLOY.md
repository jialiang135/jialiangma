# 服务器部署指南

本文档说明如何将「个人数字分身」部署到云服务器。

---

## 前置准备

### 1. 服务器选购

| 平台 | 推荐配置 | 参考价格 | 适合场景 |
|------|---------|---------|---------|
| **阿里云 ECS** | 2C4G + 40G SSD | ~60 元/月（学生更低） | 国内面试演示首选 |
| **腾讯云轻量** | 2C2G + 50G SSD | ~50 元/月 | 预算紧张的替代 |
| **AWS EC2** t3.small | 2C2G | 免费层 12 月 | 海外场景 |

系统选 Ubuntu 22.04 LTS 或 CentOS 7.9。

### 2. 服务器初始化

```bash
# SSH 登录
ssh root@<你的服务器IP>

# 安装 Docker（Ubuntu）
curl -fsSL https://get.docker.com | bash
apt install docker-compose-plugin -y

# 验证安装
docker --version
docker compose version

# 配置防火墙（开放 80/443 端口）
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 22/tcp
ufw enable
```

> 阿里云 / 腾讯云还需要在**安全组**中放行 80、443 端口。

---

## 部署步骤

### 方式一：Git 拉取（推荐）

```bash
# 1. 服务器上克隆项目
cd /opt
git clone <你的仓库地址> personal-agent
cd personal-agent

# 2. 配置环境变量
cp config/.env.example config/.env
vim config/.env  # 填入真实 API Key

# 3. 一行启动
docker compose up -d --build

# 4. 验证
curl http://localhost:80/api/health
# → {"status":"ok","version":"1.0.0"}
```

### 方式二：本地打包上传

```bash
# 本地
docker compose build
docker save personal-agent:latest | gzip > personal-agent.tar.gz
scp personal-agent.tar.gz docker-compose.yml nginx/ root@<IP>:/opt/personal-agent/

# 服务器上
cd /opt/personal-agent
docker load < personal-agent.tar.gz
docker compose up -d
```

---

## HTTPS 配置

### 使用 Let's Encrypt 免费证书

```bash
# 安装 certbot
apt install certbot -y

# 用 standalone 方式获取证书（先停掉占用 80 端口的服务）
docker compose stop nginx
certbot certonly --standalone -d your-domain.com
docker compose start nginx

# 证书路径（certbot 默认）
# 证书: /etc/letsencrypt/live/your-domain.com/fullchain.pem
# 私钥: /etc/letsencrypt/live/your-domain.com/privkey.pem
```

然后编辑 `nginx/nginx.conf`：
1. 取消 HTTPS server 块的注释
2. 填入证书路径
3. 更新 `server_name` 为你的域名

```bash
docker compose restart nginx
```

### 证书自动续签

```bash
# 添加 crontab 定时任务
echo "0 3 * * * certbot renew --quiet && docker compose -f /opt/personal-agent/docker-compose.yml restart nginx" | crontab -
```

---

## 日常运维

```bash
# 查看运行状态
docker compose ps

# 查看日志
docker compose logs -f app        # 后端日志
docker compose logs -f nginx      # Nginx 日志

# 重启服务
docker compose restart app

# 更新代码
git pull
docker compose up -d --build

# 数据备份
tar -czf backup-$(date +%Y%m%d).tar.gz assets/

# 磁盘空间
docker system prune -a  # 清理无用镜像（谨慎使用）
```

---

## 目录说明

```
/opt/personal-agent/
├── docker-compose.yml   # 编排文件
├── Dockerfile           # 镜像构建
├── nginx/
│   └── nginx.conf       # Nginx 反向代理配置
├── config/
│   └── .env             # 环境变量（不入 git，服务器上手写）
├── assets/              # 持久化数据（ChromaDB + SQLite + 上传文件）
└── logs/                # 运行日志
```

---

## 常见问题

### Q: 启动后访问 80 端口无响应？
```bash
# 检查容器状态
docker compose ps
# 检查安全组/防火墙是否放行 80 端口
# 确认 nginx 容器 health check 通过
```

### Q: ChromaDB 报错？
```bash
# 确保 assets 目录有写权限
docker compose exec app ls -la /app/assets
```

### Q: API Key 如何安全存储？
- **不要**把 `.env` 提交到 Git
- 服务器上直接在 `config/.env` 里手写
- 或用 Docker secrets（生产环境推荐）
