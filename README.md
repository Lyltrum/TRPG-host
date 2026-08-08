<p align="center">
  <img src="https://img.shields.io/badge/frontend-React_19_|_Vite_7-61dafb?style=flat-square" alt="React 19 and Vite 7" />
  <img src="https://img.shields.io/badge/backend-FastAPI_|_Python_3.13-teal?style=flat-square" alt="FastAPI and Python 3.13" />
  <img src="https://img.shields.io/badge/realtime-WebSocket-7050a0?style=flat-square" alt="WebSocket" />
  <img src="https://img.shields.io/badge/rules-CoC_7e-8b3a3a?style=flat-square" alt="Call of Cthulhu 7th Edition" />
</p>

# 🎲 TRPG-master

> **有人就能跑。** 面向线下桌游聚会的 AI 守秘人（KP）：玩家用手机加入，系统管身份、线索、私密信息与状态，AI 负责旁白、NPC、裁定、答疑和复盘。

MVP 锚定 **CoC 7 版线索调查向短模组**——收敛的线索链，不是开放叙事。当前版本已经能从注册一路跑到结局：建卡、进房、AI 守秘人主持、服务端权威掷骰、分头行动与私密信息、结束后复盘。

## 当前功能

| 模块 | 实现 |
| --- | --- |
| 账号 | 注册、登录、退出、个人信息与昵称修改；身份跨重连恢复 |
| 房间 | 创建/房间码加入、模组选择、玩家列表与准备、开始与结束对局、我的房间 |
| 建卡 | CoC7 建卡向导：职业与技能点两池记账、属性生成、年龄修正、背景故事生成、角色卡模板复用 |
| **AI 守秘人** | 两阶段回合制——裁决（JSON mode 低温，结构化决策）→ 执行（纯代码记账）→ 叙事（高温写故事）。八片能力：技能检定 / 理智 / 生命 / 移动 / 线索揭示 / 章节推进 / 议程 / 世界状态 |
| 掷骰 | **服务端权威**。两段式玩家掷骰：裁决发起 → 广播 `check.request` → 玩家点击 → 服务端掷 → `check.result` → 结算叙事 |
| 流式叙事 | 叙事边写边推（`narration.delta`），首字约 1.4s；纪律层按句提交 + 尾段扣留，不因流式退化 |
| 私密信息 | 可见性挂在主体上（`view(subject) → facts`）：历史、线索账本、本轮原话三处一起按受众裁剪；受众算错表现为没人收到，不退化成广播 |
| 讨论区 | 玩家之间一条守秘人看得见、但与主轴分开的讨论通道；支持语音输入 |
| AI 玩家 | 可给房间加 AI 队友，走**有限视角**（它会犯错走弯路，这是设定不是 bug） |
| **模组导入** | 上传 PDF / DOCX / DOC / TXT，后台 agent 转成与内置模组同 schema 的结构化产物；带进度与报告 UI。主持侧不知道模组从哪来 |
| 模组库 | 5 个内置模组（追书人 / 科比特先生 / 神秘渡轮 / 复足 / 死者的顿足舞）+ 用户自己导入的 |
| 三层记忆 | L1 事实账本（代码记账）/ L2 分段摘要（LLM 离线）/ L3 最近 200 条滑动窗口 |
| 复盘 | 对局摘要与完整事件记录 |
| API SDK | 前端唯一网络出口，封装 REST + WebSocket；与后端 DTO 对应的类型由 codegen 生成并进 git |

规模参考：26 张表、12 个 Alembic 迁移、38 个 REST 端点、18 个前端路由、78 个后端测试文件、4 个 CI workflow。

### 已知边界

- **模组导入**：整幕材料偶尔会落进不可达的槽；导入管线暂不产 `facts`，线索账本与 `reveals` 需另跑 `trpg-backend/scripts/module_probe/migrate_facts.py`。
- **长战役**：世界状态里仍有模型现编的自由文本键，长线会线性膨胀且难裁剪；schema 的章节层级（v4）尚未做。
- **裁决那一拍无法流式**（JSON mode 要完整对象才能解析），它占首字延迟的大头。
- **分头叙事是串行的**，N 组就是 N 次完整往返，尚未并行化。
- **不做 handout / 地图**：守秘人只看得见文本，发出去也没法讨论、裁定、引用。
- 部分叙事纪律靠 prompt 约束而非代码强制，属**概率性改进**——能用代码确定性判断触发条件的一律代码强制，纯语义判断的只能靠 prompt + 可观测日志。

## 系统结构

```text
trpg-frontend (React)
        │
        ▼
trpg-sdk (REST + WebSocket)          ← 前端唯一网络出口
        │
        ▼
trpg-backend (FastAPI)
        ├── /api/v1/*       REST API
        ├── /ws/{roomId}    房间实时通道
        ├── SQLAlchemy Async + Alembic   账号 / 房间 / 角色 / 事件 / 模组
        └── app/core/
            ├── keeper/          AI 守秘人（按能力垂直切片，见其 ARCHITECTURE.md）
            ├── module_import/   模组导入管线
            ├── coc7/            规则权威：规则的定义与裁决都在后端
            └── narration/       叙事生成
```

**规则权威在后端**：前端只做即时反馈的预演，且预演用的每个参数都必须来自后端——判定与展示共用同一份常量。

统一 REST 响应格式：

```json
{
  "success": true,
  "data": {},
  "error": null
}
```

WebSocket 使用独立事件信封：客户端发送 `{ "type", "playerId", "payload" }`，服务端发送 `{ "type", "payload" }`。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | React 19、TypeScript 5、Vite 7、Tailwind CSS 3、Zustand 5、React Router 7 |
| SDK | TypeScript、Rollup 4 |
| 后端 | Python 3.13、FastAPI、Pydantic 2、SQLAlchemy Async、Alembic、Uvicorn |
| 实时通信 | WebSocket |
| 数据与安全 | SQLite（本地）、PostgreSQL 异步驱动（生产）、bcrypt |
| LLM | DeepSeek；`app/core/llm_tape.py` 可录制/回放模型往返，断网重放断言代码行为不变 |
| 工程质量 | pytest、ruff、ty、Playwright（SDK→后端 e2e）、GitHub Actions |

## 项目目录

```text
TRPG-master/
├── trpg-frontend/        # 移动端 React 应用
├── trpg-sdk/             # 前后端通信 SDK，前端通过 file:../trpg-sdk 引用
├── trpg-backend/         # FastAPI 服务、REST API、WebSocket、测试
├── e2e/                  # SDK → 后端端到端（无浏览器，后端跑在 8099）
├── .github/workflows/    # 四个独立 CI：后端、SDK、前端、e2e
├── docs/                 # 守秘人设计文档与执行规格，gitignored（只在本机）
├── 模组资料/              # 第三方模组正文，gitignored（版权）
└── README.md
```

> 🔴 **版权红线**：第三方模组正文 / PDF / 结构化产物只许留在 `模组资料/`（以及 `MODULE_IMPORT_DIR` 指向的目录）。不许进 git 跟踪文件、commit message、设计文档正文或评测报告。

## 本地运行

### 环境要求

- Git
- Node.js 与 npm（版本需支持 Vite 7）
- Python 3.12 或更高版本；仓库的 `.python-version` 当前指定 3.13
- 推荐安装 [uv](https://docs.astral.sh/uv/) 管理后端环境

### 1. 克隆仓库

```bash
git clone git@github.com:Lyltrum/TRPG-host.git
cd TRPG-host
```

### 2. 构建 SDK

前端通过 `file:../trpg-sdk` 引用 SDK，因此首次启动前需要先生成 `dist`。

```bash
cd trpg-sdk
npm ci
npm run build
cd ..
```

> 🔴 **改了 `trpg-sdk/src` 之后必须重跑 `npm run build`**，否则前端 Vite 吃的是旧 `dist`，症状会被前端错误层误报成"网络连接失败"。

### 3. 启动后端

```bash
cd trpg-backend
uv sync --locked
uv run alembic upgrade head   # 建表：首次启动、以及之后表结构有变更时都要先跑
uv run uvicorn app.main:app --reload
```

> 建表由 Alembic 迁移负责（不在应用启动的 lifespan 里 `create_all`）。跳过
> `alembic upgrade head` 直接启动会因为表不存在、种子数据写入失败而崩溃。
>
> 加了新迁移之后**记得升本地开发库**——测试建表走 `create_all` 不经过迁移，
> 所以全套测试照样绿，唯一会撞上的是真正启动后端的人。升之前先备份，
> `app.db` 不在 git 里。

后端默认地址：<http://127.0.0.1:8000>

- 健康检查：<http://127.0.0.1:8000/api/v1/health>
- Swagger API 文档：<http://127.0.0.1:8000/docs>
- ReDoc API 文档：<http://127.0.0.1:8000/redoc>

复制 `.env.example` 为 `.env` 后可以覆盖默认配置；不复制也可以使用代码内置的本地开发默认值。**不配 `DEEPSEEK_API_KEY` 时叙事回退到确定性占位文案**（CI / e2e 就是这么跑的），配了才会启用真实的 AI 守秘人。

### 4. 启动前端

另开一个终端：

```bash
cd trpg-frontend
npm ci
npm run dev
```

浏览器打开：<http://localhost:9877>

默认后端 CORS 配置允许 `http://localhost:9877`。如果修改前端地址或端口，需要同步调整后端的 `CORS_ORIGINS`——注意浏览器把 `localhost` 和 `127.0.0.1` 视为不同源，两个都要放行。

## 环境变量

### 后端 `trpg-backend/.env`

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `development` | 运行环境：`development`、`production` 或 `test` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./app.db` | SQLAlchemy 异步数据库地址 |
| `ENABLE_DOCS` | `true` | 是否开放 `/docs`、`/redoc` 和 `/openapi.json` |
| `LOG_LEVEL` | `INFO` | 后端日志级别 |
| `CORS_ORIGINS` | `["http://localhost:9877"]` | 允许跨域访问的前端来源列表 |
| `DEEPSEEK_API_KEY` | 未设置 | 不配则叙事走确定性占位文案；配了启用真实 AI 守秘人 |
| `KEEPER_MODULES_DIR` | 仓库 `模组资料/` | 结构化剧本 JSON 所在目录，房间选中的模组经 catalog 映射到该目录下的文件 |
| `KEEPER_MODULE_PATH` | 未设置 | 可选兜底单文件，房间未选中 catalog 里的模组时用它 |
| `MODULE_IMPORT_DIR` | 未设置 | 模组导入的上传件与中间产物落脚点。🔴 里面全是第三方正文，必须在仓库之外或已 gitignore |
| `MODULE_IMPORT_MAX_CONCURRENT` | `2` | 同时在跑的导入任务上限 |
| `ACTION_LOCK_TIMEOUT_SECONDS` | `60` | 房间行动锁超时。守秘人一轮回应要跑多跳调用，keeper 模式建议配 `180` |
| `KEEPER_HEARTBEAT_ENABLED` | 仅 `development` 自动开 | 世界心跳：没人说话时世界也会动 |

### 前端 `trpg-frontend/.env`

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000/api/v1` | REST API 根地址；WebSocket 地址由 SDK 自动推导 |

## 构建与检查

### 后端

```bash
cd trpg-backend
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

### SDK

```bash
cd trpg-sdk
npm ci
npm run lint
npm run typecheck
npm run build
npm test
```

### 前端

```bash
cd trpg-frontend
npm ci
npm run lint
npm run build   # 内部先跑 tsc -b 做类型检查，再用 vite build 打包
npm run test
```

### e2e

```bash
cd e2e
npm ci
npm run test:e2e
```

**不需要先手动起后端**——`scripts/run-e2e.ts` 会起后端（端口 8099）、等就绪、跑测试、收摊，本地和 CI 走同一条路径。详见 [`e2e/README.md`](e2e/README.md)。

## 类型生成（codegen）

`trpg-sdk/src/generated/dto.ts` 里跟后端 DTO 对应的 TS 类型，是从
`trpg-backend/app/dto/*.py` 的 Pydantic 模型自动生成的，不再手写。**改了后端
DTO（REST 请求/响应体，或 `app/dto/ws.py` 里的 WebSocket 事件 payload）之后**，
需要依次跑：

```bash
# 1. 后端：把 DTO 导出成 JSON Schema（临时中间产物，不进 git）
cd trpg-backend
uv run python scripts/export_schema.py

# 2. SDK：从 JSON Schema 生成 TS 类型，写入 src/generated/dto.ts
cd ../trpg-sdk
npm run codegen
```

然后把 `trpg-sdk/src/generated/dto.ts` 的改动**跟 DTO 改动一起提交**——这个
文件是生成产物但会进 git（跟 `dist/` 不同：`dist/` 的消费者是机器，这个文件
的消费者是人和 CI）。忘记重新生成会被 Backend CI 的 `codegen-drift` job 拦下。

> 🔴 **DTO 别为了"构造方便"给默认值**：给了默认值，生成的 TS 契约就变成可选的，
> 前端只能写 `?? 0` 兜底——那正是最该避免的静默兜底。库里非空、服务端每次都送得出来的
> 字段，契约就该说它一定在。

## 持续集成

`.github/workflows/` 下有四个互相独立的 workflow，各自按路径过滤器触发，只有真正改到对应目录才会跑：

| Workflow | 触发路径 | 检查内容 |
| --- | --- | --- |
| `trpg-backend-ci.yml`（Backend CI） | `trpg-backend/**`；另外 `trpg-sdk/scripts/generate-types.ts`、`trpg-sdk/src/generated/**`、SDK 的 `package.json` 也会触发 | `ruff check`、`ruff format --check`、`ty check`、`pytest`；另有 `codegen-drift` job：重跑一遍 DTO → JSON Schema → TS 生成管线，用 `git diff` 确认 `trpg-sdk/src/generated/` 跟提交的一致 |
| `trpg-sdk-ci.yml`（SDK CI） | `trpg-sdk/**` | `npm run lint`、`npm run typecheck`、`npm run build` |
| `trpg-frontend-ci.yml`（Frontend CI） | `trpg-frontend/**`、`trpg-sdk/**` | `npm run lint`、`npm run build` |
| `e2e-ci.yml`（E2E CI） | `trpg-backend/**`、`trpg-sdk/**`、`e2e/**` | 起后端跑 SDK → 后端端到端用例 |

两个路径过滤器是特意放宽的，都为了堵同一类漏洞：

- **Backend CI 额外盯 `trpg-sdk/` 的几条路径**——`codegen-drift` 要在"改了 DTO 却忘记重新生成"的那个改动上就亮红灯，而 SDK CI 只在 `trpg-sdk/**` 变化时触发，一个纯改后端 DTO 的改动根本不会碰 `trpg-sdk/**`。
- **Frontend CI 也盯 `trpg-sdk/**`**——前端通过 `file:../trpg-sdk` 依赖 SDK，SDK 改一个类型就能让前端构建失败，只按前端目录过滤的话，一个纯 SDK 的改动可以在前端已经跑不起来的情况下绿灯通过。

## 开发约定

- Commit message 遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)。
- 后端 DTO（REST 或 WebSocket）发生变化时，按上面「类型生成（codegen）」的步骤重新生成 `trpg-sdk` 的类型并把生成结果一起提交，不再手动改 `trpg-sdk/src/types.ts`。
- 改了 `trpg-sdk/src` 一定要 `npm run build`；改了 `app/core/coc7/content.py` 要删掉 `app.db` 重新迁移，否则前端拿到的是旧规则。
- AI 守秘人的架构入口是 [`trpg-backend/app/core/keeper/ARCHITECTURE.md`](trpg-backend/app/core/keeper/ARCHITECTURE.md)——加一片能力 = 新建一个目录 + 注册一行，骨架代码不用改。
