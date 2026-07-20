# ============================================
# 个人数字分身 · 多Agent私有RAG系统
# Dockerfile — Multi-stage build
# ============================================

# ---------- Stage 1: Build Vue 3 frontend ----------
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

# 安装系统依赖（chromadb 需要 libgcc，OCR 需要 tesseract）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-chi-tra \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制并安装 Python 依赖（利用 Docker 层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端代码（目录级 COPY，新增文件自动包含）
COPY main.py ./
COPY config/ ./config/
COPY core/ ./core/
COPY api/ ./api/
COPY agent/ ./agent/
COPY rag/ ./rag/

# 从前端构建阶段复制构建产物
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# 创建存储目录（运行时通过 volume 持久化）
RUN mkdir -p /app/assets /app/logs

# 创建非 root 用户
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appgroup /app

USER appuser

EXPOSE 7863

CMD ["python", "main.py"]
