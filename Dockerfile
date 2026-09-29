# ============================================
# 个人数字分身 · 多Agent私有RAG系统
# Dockerfile — Multi-stage build
# ============================================

# ---------- 前端产物来源（二选一，默认用已提交的 dist）----------
#
# 默认走 `frontend-prebuilt`：直接用**仓库里已提交的** frontend/dist。
#   理由是它本来就随代码提交（本地的 npm run build 产物），在镜像里再构建
#   一遍是重复劳动 —— 而且是最重的重复劳动：
#   `npm ci` + `vite build` 峰值要 1~1.5G 内存，在 2G 的小机器上会被
#   OOM Killer 打断（表现为构建到一半无故失败、日志里看不出原因）。
#
# 下面的 `frontend-builder` 阶段保留着，需要从源码重建时（例如刚改了前端
# 但还没提交 dist）把文末 `COPY --from=frontend-prebuilt` 改成它的名字即可。

FROM scratch AS frontend-prebuilt
COPY frontend/dist /app/frontend/dist

FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

# 复制前端 manifest，利用 Docker 层缓存加速
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# 复制前端源代码并构建
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: Python backend + serve ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 安装系统依赖（使用阿里云 Debian 镜像源加速）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-chi-tra \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制并安装 Python 依赖
COPY requirements.txt ./
#
# ⚠️ **源的选择很关键，别换回 mirrors.aliyun.com**：
#
# 实测（同一台腾讯云机器、同一个 22.5MB 的 wheel、都用 HTTP/1.1）：
#     腾讯云 mirrors.cloud.tencent.com : 152 MB/s（0.2 秒下完）
#     清华   pypi.tuna.tsinghua.edu.cn  :  16 MB/s
#     中科大 mirrors.ustc.edu.cn        :  14 MB/s
#     阿里云 mirrors.aliyun.com         :  34 kB/s   ← 慢 4500 倍
#
# 阿里云那个源对 **HTTP/1.1** 限速到 34 kB/s。而同一条 URL 用 curl 走
# HTTP/2 能跑到 6.5 MB/s —— 说明是它对 HTTP/1.1 做了限速，不是带宽问题。
# Python 的 urllib/pip **只会 HTTP/1.1**，所以只要用阿里云源就必然龟速，
# 整轮构建 20 多分钟几乎全耗在这里。
#
# 这个源是公开可解析的，在腾讯云机器上会走内网（所以特别快），
# 在别处构建也能用，只是没那么快。
#
# 另外用 BuildKit 的 cache mount 而不是 `--no-cache-dir`：镜像层缓存只在
# 该层成功后才保留，这个 RUN 一旦失败，下载的东西全丢、下次从零重来；
# cache mount 独立于镜像层，**构建失败也保留**。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -i https://mirrors.cloud.tencent.com/pypi/simple/ \
                --trusted-host mirrors.cloud.tencent.com \
                -r requirements.txt

# 复制后端代码（目录级 COPY，新增文件自动包含）
COPY main.py ./
COPY config/ ./config/
COPY core/ ./core/
COPY api/ ./api/
COPY agent/ ./agent/
COPY rag/ ./rag/

# 从前端阶段复制构建产物。
#
# 这里**故意写死成 `frontend-prebuilt`**（即仓库里已提交的 dist），而不是用
# `COPY --from=${ARG}` 做可切换的变量：变量展开在部分 Docker 版本上会被当成
# 字面镜像名解析而报 "invalid reference format"。要改成从源码构建时，
# 把下面这行的 stage 名换成 `frontend-builder` 即可（改一行，无歧义）。
COPY --from=frontend-prebuilt /app/frontend/dist ./frontend/dist

# 创建存储目录（运行时通过 volume 持久化）
RUN mkdir -p /app/assets /app/logs

# 创建非 root 用户
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 7863

CMD ["python", "main.py"]
