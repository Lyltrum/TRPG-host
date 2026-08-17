# 后端镜像：FastAPI + uvicorn，用 uv 装依赖。
#
# 分两个 stage 只为一件事：**编译期的东西不进最终镜像**。uv 自己、构建缓存、
# 以及安装过程中产生的中间件都留在 builder 里。

FROM python:3.13-slim AS builder

# uv 官方镜像里直接拷二进制，比 pip install uv 快且不污染 site-packages。
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# 🔴 先只拷依赖清单再装依赖，最后才拷源码。改一行业务代码时，上面这层
# 命中缓存，省掉整个依赖安装——顺序反过来的话每次改代码都要重装一遍。
COPY pyproject.toml uv.lock ./

# --locked：严格按 uv.lock 装。跟 CI 用同一个开关，避免"镜像里的版本"和
# "CI 验过的版本"是两套。
# --no-install-project：这一步只装第三方依赖，本项目自身等源码拷进来再说。
RUN uv sync --locked --no-install-project --no-dev

COPY . .

RUN uv sync --locked --no-dev


FROM python:3.13-slim AS runtime

# libpq 不需要——asyncpg 是纯 Python 实现的协议层，不依赖 libpq。
# 这里只补一个 curl，给 compose 的 healthcheck 用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 🔴 不用 root 跑。容器里 root 逃逸出去就是宿主 root，而这个进程要处理
# 公网来的请求、还要落盘用户上传的模组文件。
RUN useradd --create-home --uid 10001 aidm

WORKDIR /app

COPY --from=builder --chown=aidm:aidm /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER aidm

EXPOSE 8000

# 🔴 **不在这里跑 alembic upgrade。** 建表是一次性动作，而容器可能被拉起
# 多份（重启、扩容）——并发跑迁移会互相踩。compose 里用一个一次性的
# `migrate` 服务做这件事，应用容器等它跑完再起。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
