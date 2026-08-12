"""应用配置。

用 pydantic-settings 从环境变量 / `.env` 文件里读配置，而不是散落在代码各处的
硬编码常量或裸 `os.environ.get(...)`——好处是每个配置项都有类型、默认值和校验，
IDE 能补全，写错类型（比如 ENABLE_DOCS 传了个不是 true/false 的字符串）会在启动时
就报错，而不是运行到一半才炸。
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 私有网段（RFC 1918）+ 本机的 Origin，任意端口。给 CORSMiddleware 的
#: `allow_origin_regex` 用。
#:
#: 🔴 **不用 `*`**：`allow_credentials=True` 时通配符本来就非法（浏览器直接拒），
#: 而且"允许任何来源"和"允许同一间屋子里的设备"是两件事。这条正则只认
#: 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16 与 localhost/127.0.0.1——一台
#: 公网页面拿不到这样的 Origin。
#:
#: 只认 `http://`：局域网里没有证书，走的必然是明文。以后真上了公网要配的是
#: `CORS_ORIGINS` 那张显式清单，不是把这条正则放宽。
PRIVATE_NETWORK_ORIGIN_REGEX = (
    r"^http://("
    r"localhost"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")(:\d+)?$"
)


class Settings(BaseSettings):
    # env_file=".env"：本地开发时从 backend 目录下的 .env 文件读取（该文件已被
    # .gitignore 排除，不会进 git）；线上部署通常直接注入真实环境变量，.env 不存在也没关系。
    # extra="ignore"：.env 里出现未在下面声明的字段时不报错，方便同一份 .env
    # 文件里塞一些暂时用不到、以后可能会用的变量。
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # development：本地开发（默认）；production：线上；test：预留给测试环境用，
    # 目前测试套件是通过 fixture 直接覆盖依赖注入，不依赖这个值。
    app_env: Literal["development", "production", "test"] = "development"

    # 本地默认用 SQLite（aiosqlite 驱动），不需要额外起数据库就能跑通整个项目；
    # 线上把这个环境变量换成 PostgreSQL 的连接串（asyncpg 驱动）即可切换，
    # 业务代码（models/service）完全不用改，因为都是通过 SQLAlchemy ORM 访问的。
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # 是否开启 FastAPI 自带的 /docs、/redoc、/openapi.json。本地开发默认开，
    # 线上环境建议在环境变量里设为 false，避免把接口细节暴露给外部。
    enable_docs: bool = True

    # structlog 的最低日志级别，比如 "DEBUG"/"INFO"/"WARNING"。
    log_level: str = "INFO"

    # 允许跨域请求的前端来源列表，交给 main.py 里的 CORSMiddleware 使用。
    # 本地默认放行 Vite 开发服务器的默认端口 9877。
    cors_origins: list[str] = ["http://localhost:9877"]

    # 🔴 局域网开局：朋友用手机从 `http://192.168.x.x:9877` 打开时，浏览器发出的
    # Origin 是那个 IP，**不在上面那张固定清单里**，请求会被 CORS 挡掉。
    #
    # 默认 **True**：这个项目的定位就是"自己和朋友在一间屋子里玩"，默认关掉等于
    # 邀请链接做了也用不了（同 `exec/35` 那条——链接指向 localhost 就等于没做）。
    # 放行范围**只有私有网段**（见 `PRIVATE_NETWORK_ORIGIN_REGEX`），不是 `*`；
    # 真要收紧就把它设成 false。
    cors_allow_private_network: bool = True

    # DeepSeek API Key（issue #107 地基，`app/core/narration/`）：配了就走真实
    # DeepSeek 生成叙事回应，不配（默认）自动回退到确定性的占位文案——CI/e2e
    # 环境不配这个变量，本地演示/线上环境按需配置。
    deepseek_api_key: str | None = None

    # keeper agent（feat/keeper-agent 实验）：配 deepseek_api_key 后启用。
    # - keeper_modules_dir：structured JSON 所在目录（默认仓库 `模组资料/`）；
    #   房间选中的 scenario 经 catalog 映射到该目录下文件。
    # - keeper_module_path：可选兜底单文件；房间未选中 catalog 模组时用它。
    # 剧本文件 gitignore（版权），不进公开仓库。
    keeper_modules_dir: str | None = None
    keeper_module_path: str | None = None

    # 模组导入（`exec/29` 第 5 步）：用户上传件与中间产物的落脚点。
    # 🔴 里面全是第三方模组正文，与 `模组资料/` 同级红线——目录必须在仓库之外
    # 或已 gitignore；不进 git / 日志 / 磁带。
    module_import_dir: str | None = None
    # 同时在跑的导入任务上限。一次导入 ≈ 71 次 LLM 调用，没有闸门的话几个人
    # 同时上传就会把 provider 那边排满、把本进程拖垮。
    module_import_max_concurrent: int = 2
    # 🔴 转换失败时保留中间产物（裸抽取 / 关系 / 组装中间态 / 校验报告）。
    # 默认删：那些文件含第三方正文，而且用户端没人会去看。开发排查时打开——
    # 失败现场删掉了就只能靠再花一次 ¥0.35 重现，而结果还未必一样（拒绝率有
    # 运气成分，实测同一份文件四次跑出三种不同结果）。
    module_import_keep_work: bool = False

    # action.submit 房间锁的超时兜底秒数。keeper agent 一轮回应要跑多跳工具
    # 调用（30-90s），60s 会让锁在正常裁决中途过期、放另一个玩家插进来打断——
    # keeper 模式建议配 180。
    action_lock_timeout_seconds: float = 60.0

    # 世界心跳（设计 05 / 路线 6）：development 默认真·试玩要世界会动；
    # production/test 默认关（CI/e2e 不烧 token、不抢锁）。可用环境变量覆盖。
    # 未显式配置时：仅 development 自动开。
    keeper_heartbeat_enabled: bool | None = None
    keeper_heartbeat_silence_seconds: float = 90.0
    keeper_heartbeat_min_interval_seconds: float = 180.0
    keeper_heartbeat_scan_interval_seconds: float = 30.0
    keeper_heartbeat_max_consecutive: int = 2

    def heartbeat_enabled(self) -> bool:
        if self.keeper_heartbeat_enabled is not None:
            return self.keeper_heartbeat_enabled
        return self.app_env == "development"

    # ⚠️ 测试专用（issue #107）：让叙事生成人为延迟 N 秒后再返回，生产永远保持 0。
    # 存在的理由：无 key 时的占位叙事同步秒回，action.submit 的房间锁窗口只有
    # 微秒级，e2e 两个客户端"同时提交"永远压不中 ACTION_IN_PROGRESS——锁的
    # 并发拒绝路径会变成测不到的死代码。e2e 起后端时把它设成 1~2 秒，锁窗口
    # 就能被稳定命中。
    narrator_delay_seconds: float = 0.0


@lru_cache
def get_settings() -> Settings:
    """获取全局唯一的 Settings 实例。

    加 @lru_cache 是因为 Settings() 在实例化时会去读环境变量/.env 文件，
    没必要每次调用都重新读一遍磁盘——缓存下来，全进程共享同一份配置对象。
    """
    return Settings()
