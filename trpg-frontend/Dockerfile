# 前端镜像：把 SDK 和前端都构建出来，最终只留一份静态文件 + nginx。
#
# 🔴 **构建上下文是仓库根，不是 trpg-frontend/**。前端用 `file:../trpg-sdk`
# 依赖 SDK，上下文设成 trpg-frontend/ 的话那条相对路径指到上下文外面，
# `npm ci` 直接失败。compose 里对应 `context: .` + `dockerfile:` 指到这里。

FROM node:22-slim AS builder

WORKDIR /src

# ── SDK 先构建 ────────────────────────────────────────────────
# 前端吃的是 SDK 的 dist/，不是 src/。漏了这一步，前端要么构建失败，
# 要么（更糟）吃到一份陈旧的 dist——那正是本地开发反复踩的坑，
# 症状会被前端错误层误报成"网络连接失败"。
COPY trpg-sdk/package.json trpg-sdk/package-lock.json ./trpg-sdk/
RUN cd trpg-sdk && npm ci

COPY trpg-sdk ./trpg-sdk
RUN cd trpg-sdk && npm run build

# ── 再构建前端 ────────────────────────────────────────────────
COPY trpg-frontend/package.json trpg-frontend/package-lock.json ./trpg-frontend/
RUN cd trpg-frontend && npm ci

COPY trpg-frontend ./trpg-frontend

# 🔴 这里**不设** VITE_API_BASE_URL。生产构建下 `api-client.ts` 会自己派生成
# 同源地址（见那里的注释）。钉死它反而会把部署锁死在某一个域名上——同
# 「局域网开局时 .env 必须保持注释」那条判据。
RUN cd trpg-frontend && npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=builder /src/trpg-frontend/dist /usr/share/nginx/html
COPY trpg-frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
