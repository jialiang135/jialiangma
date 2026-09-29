# 服务器部署指南（腾讯云轻量 · 当前生效）

本文档说明如何把「个人数字分身」部署到**当前在用的**服务器。
文中每一条"坑"都是这次腾讯云部署真实踩到并修掉的，不是罗列可能性。

---

## 一、服务器信息

| 项 | 值 |
|---|---|
| 平台 | 腾讯云轻量应用服务器（2C2G 起步即可，应用常驻约 500~700M） |
| 公网 IP | **193.112.29.164** |
| 登录用户 | `ubuntu`（免密 sudo） |
| 项目路径 | `/opt/personal-agent` |
| 登录方式 | `ssh ubuntu@193.112.29.164`（**已装公钥，不用密码**） |
| 对外端口 | Docker 里的 nginx 同时映射 **80 和 8080**（都指向容器内的 80） |
| 应用监听 | 容器内 `7863`（仅 `expose`，不映射到宿主机） |

> 旧的阿里云 ECS `39.106.191.98`（用户 `admin`）**已到期释放**，不要再用。
> 文档里若出现它，一律是过期信息。

首次部署/拉代码前，确认连通：

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://193.112.29.164:8080/api/health
# → HTTP 200
```

---

## 二、部署流程（固定套路）

**打包 → 上传到 `/tmp/` → 解压 → 停旧服务 → 替换文件 → 启动新服务 → 验证。**

本地打包并直接经 SSH 管道送达服务器（服务器上的 `git pull` 从国内访问 GitHub
HTTPS 经常超时，所以用 tar+SSH，不走 git）：

```bash
cd e:/zuoye/jialiangma/personal_agent && tar czf - \
  --exclude='config/.env' --exclude='node_modules' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='assets' --exclude='logs' \
  --exclude='frontend/node_modules' --exclude='frontend/dist' \
  api/ core/ config/ agent/ rag/ frontend/ scripts/ nginx/ .github/ \
  requirements.txt Dockerfile docker-compose.yml main.py \
  | ssh ubuntu@193.112.29.164 \
    "cd /opt/personal-agent && tar xzf - && docker compose up -d --build app && echo 'deploy ok'"
```

只改了静态文件（README、nginx 配置、docker-compose）时无需重建，直接秒传：

```bash
cd e:/zuoye/jialiangma/personal_agent && tar czf - <file1> <file2> \
  | ssh ubuntu@193.112.29.164 "cd /opt/personal-agent && tar xzf - && echo 'sync ok'"
```

验证：

```bash
ssh ubuntu@193.112.29.164 "docker compose -f /opt/personal-agent/docker-compose.yml ps"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://193.112.29.164:8080/api/health
```

---

## 三、构建/启动踩过的坑（务必先看）

### 1. pip 必须用腾讯云内网源

阿里云 PyPI 对 **HTTP/1.1 限速到 34 kB/s**，而 **pip 只会 HTTP/1.1**（同一条 URL 用
curl 走 HTTP/2 能跑到 6.5 MB/s —— 说明是它对 HTTP/1.1 做了限速，不是带宽问题）。

同一台腾讯云机器、同一个 22.5MB 的 wheel、都用 HTTP/1.1 实测：

| 源 | 速度 |
|---|---|
| 腾讯云 `mirrors.cloud.tencent.com` | **152 MB/s** |
| 清华 `pypi.tuna.tsinghua.edu.cn` | 16 MB/s |
| 中科大 `mirrors.ustc.edu.cn` | 14 MB/s |
| 阿里云 `mirrors.aliyun.com` | **34 kB/s**（慢约 4500 倍） |

用阿里云源时整轮构建 20+ 分钟几乎全耗在下载；换腾讯云源后降到**十几秒**。
Dockerfile 里已经写死腾讯云源，**不要**换回 `mirrors.aliyun.com`。

### 2. 绑定挂载目录的属主必须是容器用户（uid 999）

应用以非 root 用户 `appuser` 运行，其 **uid=999**。而 `docker-compose.yml` 把
`./logs`、`./assets` 绑定挂载进容器 —— 这两个目录若属主是宿主机用户，容器以
uid 999 写不进去，启动即崩：

```
PermissionError: /app/logs/app_*.log
```

修法（在服务器上执行）：

```bash
sudo chown -R 999:999 /opt/personal-agent/logs /opt/personal-agent/assets
```

### 3. 一次只跑一个 build

同时起两个 `docker compose ... --build` 会抢**同一个 BuildKit 缓存锁**，两边一起干等
（典型表现：负载只有 0.07，根本没在干活，但一直不结束）。等一个跑完再起下一个。

### 4. Dockerfile 里 pip 用 BuildKit cache mount，不是 `--no-cache-dir`

镜像层缓存**只在该层成功后才保留**，这一层一旦失败，下载的东西全丢、下次从零重来；
cache mount 独立于镜像层，**构建失败也保留**。所以 Dockerfile 用的是：

```dockerfile
RUN --mount=type=cache,target=/root/.cache/pip pip install -i https://mirrors.cloud.tencent.com/pypi/simple/ ...
```

### 5. 依赖冲突（已修，改依赖时勿踩回去）

- **`prometheus-fastapi-instrumentator` 必须钉 7.x**（现为 `7.1.0`）。
  8.0.0+ 要求 `starlette>=1.0.0`，而 `fastapi 0.115.6` 要求 `starlette<0.42.0` ——
  两者互斥，pip 直接 `ResolutionImpossible`，**整份 requirements 一个都装不上**。
  "本机能跑"是因为那些包是历史上一个个装上去的，pip 从没一次性解过整张表。
- **`ragas` 拆到 `requirements-eval.txt`**。它的 `instructor` 要求 `jiter<0.15`，
  与 `openai`（经 `langchain-openai`）要求的 `jiter>=0.16` 死锁，合在一起会让主依赖
  也装不上。评测因此是**可选功能**：不装 ragas 时应用照常跑，只是执行评测会提示依赖未安装。

### 6. `config/.env` 在服务器上单独维护

- `config/.env` 含真实 Key，**只在服务器上维护**，**永远不要**打进 tar 包。
- **JWT 密钥与本地不同**（部署时重新生成），所以本地签发的 token 到线上无效，属正常。
- 公网部署前想清楚 `ALLOW_REGISTRATION` 的取舍（开放注册 = 任何人可建号）。

---

## 四、服务器操作要点

- 服务器是 **Linux 语义**：`/tmp` 是临时目录（会被清理），正式文件放 `/opt/personal-agent`，别留在 `/tmp`。
- 上传的文件记得赋权限、以正确用户运行；`ubuntu` 有免密 sudo。
- 应用容器内是 `appuser`（uid 999），**不是** root。
- 查状态 / 日志：

```bash
ssh ubuntu@193.112.29.164 "docker compose -f /opt/personal-agent/docker-compose.yml ps"
ssh ubuntu@193.112.29.164 "docker logs -f personal-agent"
```

- 监控（Prometheus + Grafana）默认不启动（常驻约 400~500M 内存），需要时显式拉起：

```bash
ssh ubuntu@193.112.29.164 "cd /opt/personal-agent && docker compose --profile monitoring up -d"
```
