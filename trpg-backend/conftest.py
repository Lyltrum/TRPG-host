"""pytest 共享 fixture：让测试完全脱离本地/生产的真实数据库，跑在一个
每次测试都从零开始的内存 SQLite 上。

核心手法是 FastAPI 的依赖覆盖（`app.dependency_overrides`）：main.py 里的
路由都是通过 `Depends(get_db)` 拿数据库会话的，这里把 `get_db` 整体替换成
指向内存数据库的版本，路由代码本身完全不用感知"现在跑的是测试"。
"""

import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.controller import ws as ws_controller
from app.core.db import Base, get_db
from app.core.seed import ensure_seed_content
from app.main import app

# 用临时文件 SQLite，不用 ":memory:"+StaticPool。关键原因是并发模型：异步 HTTP
# 测试跑在 pytest-asyncio 的事件循环里，而同步 TestClient 的 WebSocket 跑在它
# 自己 portal 线程的另一个事件循环里。aiosqlite 的每个连接都绑定在「创建它的
# 那个事件循环」上，StaticPool 只有一个共享连接——两个循环去用同一个连接会
# 直接死锁。文件型 SQLite 让每个循环各开各的连接、指向同一个文件，就不存在这个
# 问题；测试间隔离仍由下面 _prepare_database 的 create_all/drop_all 保证。
# （另外 WS 路由已改成每条消息一个短 session，不再长期持有连接、锁窗口很短，
# 见 app/controller/ws.py。）文件放临时目录，进程退出随之清理。
_TEST_DB_PATH = Path(tempfile.mkdtemp(prefix="trpg-test-")) / "test.db"
# NullPool：每次用完立刻关闭连接，不在池里留着复用。同步 TestClient 的 WS 跑在
# 它自己 portal 线程的事件循环里，如果连接被池缓存下来、下一个用例（在别的
# 循环里）又拿到这个绑定在旧循环上的连接，就会挂死。NullPool 让每次都开一个
# 干净的新连接，避免跨事件循环复用。
test_engine = create_async_engine(f"sqlite+aiosqlite:///{_TEST_DB_PATH}", poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    """跟 app/core/db.py 里的 get_db 签名一致，只是内部用的是测试专用的引擎/会话工厂。"""
    async with TestSessionLocal() as session:
        yield session


# 关键一行：把 FastAPI 依赖图里所有用到 `Depends(get_db)` 的地方，替换成上面
# 这个指向内存数据库的版本。这行代码在模块导入时就执行（而不是放在某个 fixture
# 里），所以只要 pytest 收集了这个 conftest.py，整个测试会话期间 app 用的都是
# 测试数据库，不会有测试请求不小心打到本地开发用的 SQLite 文件或线上数据库。
app.dependency_overrides[get_db] = override_get_db

# WS 路由（app/controller/ws.py）是原生 websocket handler，拿不到 FastAPI 的
# Depends(get_db)，而是直接 `async with async_session_factory() as db`——也就是
# 上面的依赖覆盖对它无效，它会绕过测试库去连真实库。这里把 ws 模块引用的那个
# 工厂也重绑到测试库，否则 WS 测试里 HTTP 请求写进的是内存测试库、WS 路由却
# 去空的真实库里查 player，必然查不到而关连接。
ws_controller.async_session_factory = TestSessionLocal  # type: ignore[assignment]

# 表只建一次。此前 `_prepare_database` 每条用例都跑一遍 create_all/drop_all，
# 实测单次 57ms + 43ms，乘以 1561 条 = **全套 314s 里的 156s**，而绝大多数用例
# 根本不碰数据库（比如 760 条只查注释里的路径存不存在的参数化用例）。
# 建表用同步引擎在导入期做，跟上面的依赖覆盖同一时机——pytest-asyncio 的事件
# 循环是函数级的，session 级的异步 fixture 会跨循环，那正是本文件两段注释里
# 反复踩过的坑。
_sync_engine = create_engine(f"sqlite:///{_TEST_DB_PATH}")
Base.metadata.create_all(_sync_engine)
_sync_engine.dispose()

# 清表按外键依赖的**反序**（sorted_tables 是「被依赖的在前」）。
_TABLES_IN_DELETE_ORDER = tuple(reversed(Base.metadata.sorted_tables))


@pytest.fixture(autouse=True)
def _no_background_writer() -> Generator[None, None, None]:
    """🔴 钉死背景生成器，默认关掉——别让开发机 `.env` 决定测试会不会打真实 API。

    配了 `DEEPSEEK_API_KEY` 的机器上 `app.state.background_writer` 是真的，而
    「一键生成」和「加 AI 队友」两条路径都会调它：任何人新写一条建卡用例，都会
    在本地悄悄发一次真请求（慢、要钱、结果还不稳定），到了 CI 上又因为没有 key
    而走另一条分支——同一份代码两个结果，正是本项目已踩三次的那个坑。

    要验生成本身的用例自己覆盖 `app.state.background_writer`（见
    `test_character_background.py`）。
    """
    previous = getattr(app.state, "background_writer", None)
    app.state.background_writer = None
    yield
    app.state.background_writer = previous


@pytest.fixture(autouse=True)
def _no_equipment_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 同上，第四次：装备合理性校验也别让开发机的 `.env` 决定要不要打真实 API。

    它跟背景生成器的区别在于**没有 `app.state` 开关**——`complete_character` 直接
    读 `get_settings().deepseek_api_key`，于是配了 key 的机器上**每一条建卡用例**
    都会真发一次请求（`BUILT_CHARACTER` 本来就带着两件装备）。症状很难认：单跑
    那条用例是绿的，全套跑就随机红一条，因为红不红取决于模型这次怎么判——
    2026-08-16 加这个功能时当场撞到，`test_full_character_build_flow_marks_player_ready`
    单跑通过、全套失败。

    要验校验本身的用例自己把它装回来（见 `tests/test_equipment_check.py::_install`）。
    """
    from app.core.config import get_settings
    from app.service import character as character_service

    without_key = get_settings().model_copy(update={"deepseek_api_key": None})
    monkeypatch.setattr(character_service, "get_settings", lambda: without_key)


@pytest.fixture(autouse=True)
async def _prepare_database() -> AsyncGenerator[None, None]:
    """每个测试函数跑之前把库清空重灌，保证测试之间互不影响（不用手动在每个
    测试里管理数据库状态）。autouse=True 表示不需要在测试函数参数里显式声明
    这个 fixture，pytest 会自动应用到每个测试上。

    表在导入期就建好了（见上），这里只清行——**语义跟 create_all/drop_all
    那版一致**（每条用例都从一个只有种子数据的空库开始），但不用每次重做 DDL。
    """
    async with test_engine.begin() as conn:
        for table in _TABLES_IN_DELETE_ORDER:
            await conn.execute(delete(table))
    # 灌内置模组种子数据：生产环境靠 main.py lifespan 的 ensure_seed_content，
    # 但 ASGITransport 测试客户端不触发 lifespan，所以这里手动灌一次——否则
    # 依赖内置模组的用例（建房选模组、GET /modules 等）会因为库里没有模组而失败。
    async with TestSessionLocal() as session:
        await ensure_seed_content(session)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """给需要绕过 HTTP、直接读写数据库的用例用（多数用例走 `client` 就够了）。

    注意别在测试模块里 `from tests.conftest import TestSessionLocal`——那会把
    conftest 当成另一个模块再导入一次、连带新建一个引擎，表建在旧引擎上，
    结果是 `no such table`。要拿 session 就用这个 fixture。
    """
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
def sql_counter() -> Generator[list[str], None, None]:
    """记录这段测试期间真实执行的 SQL 语句，用来断言"查询数不随数据量增长"。

    给 N+1 查询这类问题用：光看接口返回值是对的看不出它发了多少条查询，而 N+1
    的症状要到数据量长起来才显现——那时候再发现就晚了。断言用"总数有上限"而不是
    "等于某个具体值"，免得以后加一条无关查询就误报。
    """
    from sqlalchemy import event

    executed: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        executed.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield executed
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """给测试用例注入一个能直接调用 FastAPI app 的异步 HTTP 客户端。

    `ASGITransport(app=app)` 让 httpx 直接在内存里调用 ASGI 应用，不需要真的
    起一个监听端口的服务器进程——测试跑得更快，也不用操心端口占用。注意这种
    方式不会触发 main.py 里的 lifespan（也就是 init_db 不会被调用），但这里
    不需要它：数据库表是靠上面的 `_prepare_database` fixture 直接建的。
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
