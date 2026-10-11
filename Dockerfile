# ============================================================
# quant-harness Dockerfile
#
# 目标: 一条命令把整套系统跑起来, 不依赖宿主机装 Python / akshare / backtrader。
#
# 构建:
#   docker build -t quant-harness .
#
# 运行(前端):
#   docker run --rm -p 8501:8501 --env-file .env \
#     -v "%cd%/data:/app/data" -v "%cd%/logs:/app/logs" quant-harness
#   -> 打开 http://localhost:8501
#
# 运行(每日流水线, 跑完退出):
#   docker run --rm --env-file .env \
#     -v "%cd%/data:/app/data" -v "%cd%/logs:/app/logs" \
#     quant-harness python run_daily.py
#
# 用 docker compose 更省事, 见 docker-compose.yml
# ============================================================

FROM python:3.12-slim

# ---- 环境变量 ----
# PYTHONUNBUFFERED: 日志实时输出(否则容器里 print 会被缓冲, docker logs 看不到)
# PYTHONUTF8 / PYTHONIOENCODING: 强制 UTF-8 —— 这个项目的中文输出很多,
#   不加的话在 C locale 下会变乱码(和 Windows 下的坑是同一个问题)
# TZ: 项目强依赖 Asia/Shanghai, 容器默认是 UTC, 不设时区会导致"今天"判断错一天
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- 系统依赖 ----
# tzdata: 时区数据(不装的话 TZ 环境变量不生效)
# curl:   healthcheck 用
# 装完立刻清 apt 缓存, 否则镜像白白多几十 MB
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ----
# 先只拷 requirements.txt 再装依赖: 这样只要依赖没变, 改代码时这层缓存能复用,
# 重新构建从几分钟降到几秒(这是 Docker 分层缓存的经典用法)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 项目代码 ----
COPY . .

# ---- 运行时目录 + 非 root 用户 ----
# 为什么要非 root: 容器里以 root 跑, 挂载出来的 data/logs 文件在宿主机上
# 会是 root 所有, 之后宿主机上的脚本可能没权限改。用普通用户更干净。
RUN mkdir -p /app/data /app/logs /app/output /app/.tmp \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# ---- 端口 ----
EXPOSE 8501

# ---- 健康检查 ----
# Streamlit 自带 /_stcore/health 端点, 不用额外写探针
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# ---- 默认启动前端 ----
# --server.address=0.0.0.0 必须加: 默认只监听 127.0.0.1, 容器外访问不到
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]
