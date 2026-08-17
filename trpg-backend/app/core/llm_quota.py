"""每个账号每天能发多少次 LLM 调用——**唯一会真金白银出血的那道闸门**。

## 为什么闸门在这一层，而不是回合入口

回合入口有五处（`ws.py` 四处 + 心跳一处，各自 `async with held(...)`）。
逐个加检查就是这个项目反复吃亏的那个形状——**逐个列出的判断，加一项就漏
一项**：明天加第六个入口，它会安静地不受限。

而 `build_llm_client()` 是**唯一**的异步 LLM 出口（七个调用点全过它），把闸门
挂在那里，新增调用点自动被覆盖。代价是计的是"调用次数"而不是"回合数"——
那本来就是更接近真实成本的量。

## 为什么记账是一条原子 UPSERT

先 `SELECT` 再 `UPDATE` 是 check-then-act：两个并发回合会各读到同一个旧值、
各写回 +1，**少记一次**。这个项目在唯一约束那次已经踩过一模一样的形状。
所以走 `UPDATE ... SET calls = calls + 1` 让数据库自己加，没命中再 `INSERT`。

## 🔴 放行的那三种情况，都是显式的

**没配 key 不算，回放磁带不算，没有配额主体不算。** 前两种压根不产生费用；
第三种是"这次调用没人认领"，见 `quota_subject()` 的说明——它记 WARNING 而
不是静默放行，因为一条查不出主人的调用意味着有条路径忘了绑主体。

## 边界（别高估它）

- **只管异步那条路**（对局）。模组导入走同步客户端 `_TapedSyncCompletions`，
  它有自己的闸门 `MODULE_IMPORT_MAX_CONCURRENT`（并发数，不是总量）——
  **导入的总量目前仍然没有上限**，这是已知缺口。
- **单进程内准确**。多开一个进程时计数仍然对（数据库是共享的），但那时先撞到
  的是锁与 WS 在进程内存里这个更大的问题。
- **不是限速器**。它拦的是"一天烧掉多少"，不拦"一秒发几次"。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import UTC, date, datetime

import structlog
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.core.errors import AppException, ErrorCode
from app.models.user import LlmDailyUsage

logger = structlog.get_logger(__name__)

#: 这次调用算在谁头上。`None` = 没人认领（见模块 docstring）。
_subject: ContextVar[str | None] = ContextVar("llm_quota_subject", default=None)


class quota_subject:  # noqa: N801 — 当上下文管理器用，小写读起来才像 `with`
    """把接下来的 LLM 调用算在这个账号头上。

    用 contextvar 而不是把 `user_id` 一路传参：中间隔着 keeper 的编排层、叙事层、
    磁带层十几个函数，每一层都加一个参数等于让**每个调用点**都有机会漏传，而漏传
    的后果是静默放行。contextvar 的作用域跟着这一轮的调用栈走，天然不会串到
    别的房间。

    ⚠️ `user_id=None` 是合法的（AI 玩家、没登录的历史房间），它表示"确实没有
    主体"，与"忘了绑"在日志里长得一样——所以绑不上时**由调用方**决定要不要
    记，而不是这里。

    🔴 **同步与异步两副接口都实现了**，这样它能直接并进已有的
    `async with async_session_factory() as db, quota_subject(uid):` 那一行。
    只给同步那副的话，WS 主循环那处就得把整个 dispatch 块（几百行）往里缩进
    一级——那种改动的 diff 会淹掉真正的改动，也更容易在合并时出错。
    进出都只碰一个 contextvar，没有 IO，所以异步那副不需要做别的事。
    """

    __slots__ = ("_user_id", "_token")

    def __init__(self, user_id: str | None) -> None:
        self._user_id = user_id
        self._token: Token[str | None] | None = None

    def _enter(self) -> None:
        self._token = _subject.set(self._user_id)

    def _exit(self) -> None:
        if self._token is not None:
            _subject.reset(self._token)
            self._token = None

    def __enter__(self) -> None:
        self._enter()

    def __exit__(self, *exc: object) -> None:
        self._exit()

    async def __aenter__(self) -> None:
        self._enter()

    async def __aexit__(self, *exc: object) -> None:
        self._exit()


def current_subject() -> str | None:
    return _subject.get()


class QuotaExceeded(AppException):
    """今天的额度用完了 → 429。

    单独一个类而不是直接 `raise AppException(RATE_LIMITED, ...)`：调用方
    （尤其是 WS 那条路）要能把它跟别的 `AppException` 分开处理——额度用完
    是可以对玩家如实说的，别的内部错误不是。
    """

    def __init__(self, *, used: int, quota: int) -> None:
        super().__init__(
            ErrorCode.RATE_LIMITED,
            f"今天的 AI 额度已经用完了（{used}/{quota} 次），明天 UTC 零点恢复",
            429,
        )
        self.used = used
        self.quota = quota


async def enforce_quota(*, kind: str) -> None:
    """LLM 收口点调用的那一行：该记就记，超了就抛。

    放行的每一种情况都在这里显式写出来，**没有 else 兜底**——静默放行正是
    这道闸门最可能的坏法，而它坏了不会有任何东西变红，只会在月底的账单上。
    """
    settings = get_settings()

    quota = settings.llm_daily_call_quota
    if quota <= 0:
        # 显式关闭（配 0 或负数）。留这条是为了本地跑批量脚本时能整个摘掉，
        # 而不是让人去把闸门代码注释掉。
        return

    user_id = _subject.get()
    if user_id is None:
        # 没有主体 = 这次调用没人认领。**记 WARNING 而不是静默放行**：它意味着
        # 有一条路径忘了 `quota_subject(...)`，而那条路径就是绕过闸门的洞。
        logger.warning("llm_call_without_quota_subject", kind=kind)
        return

    await charge_one_call(user_id=user_id, quota=quota)


async def charge_one_call(*, user_id: str, quota: int) -> int:
    """记一次调用并返回**记完之后**的总数；超额则抛 `QuotaExceeded`。

    🔴 **先记再判，不是先判再记。** 判在前的话，两个并发调用会同时读到
    `quota - 1` 然后双双放行——多烧的那一次正是这道闸门存在的理由。
    先让数据库把数加上去、拿到权威的新值，再决定放不放行；超了就抛，那一次
    调用**不会发出去**，但它已经被记进去了（宁可多记一次，不可少记一次）。

    🔴 **自己开 session、自己提交，不接调用方那一个。** 记账必须独立于业务
    事务：一个回合跑到一半失败会回滚它自己的 session，而那时 LLM 调用**已经
    发出去了、钱已经花了**。搭调用方的车等于让"失败的回合免费"——而失败的
    回合恰恰是最容易被反复重试的那种。
    """
    today = datetime.now(UTC).date()

    async with async_session_factory() as session, session.begin():
        used = await _increment(session, user_id=user_id, today=today)

    if used > quota:
        logger.warning("llm_quota_exceeded", user_id=user_id, used=used, quota=quota)
        raise QuotaExceeded(used=used, quota=quota)
    return used


async def _increment(session: AsyncSession, *, user_id: str, today: date) -> int:
    result = await session.execute(
        update(LlmDailyUsage)
        .where(LlmDailyUsage.user_id == user_id, LlmDailyUsage.day == today)
        .values(calls=LlmDailyUsage.calls + 1, updated_at=datetime.now(UTC))
        .returning(LlmDailyUsage.calls)
    )
    row = result.first()

    if row is None:
        # 今天还没有这一行。并发下两个请求可能同时走到这里，唯一约束会让
        # 其中一个炸 —— 炸了就说明另一个刚建好，回到 UPDATE 那条路重来一次。
        try:
            async with session.begin_nested():
                session.add(LlmDailyUsage(user_id=user_id, day=today, calls=1))
            used = 1
        except IntegrityError:
            retry = await session.execute(
                update(LlmDailyUsage)
                .where(LlmDailyUsage.user_id == user_id, LlmDailyUsage.day == today)
                .values(calls=LlmDailyUsage.calls + 1, updated_at=datetime.now(UTC))
                .returning(LlmDailyUsage.calls)
            )
            retried = retry.first()
            if retried is None:
                # 到这儿说明既插不进去、又更新不到，是真的坏了——不兜底。
                raise
            used = int(retried[0])
    else:
        used = int(row[0])

    return used


async def usage_today(session: AsyncSession, *, user_id: str) -> int:
    """今天已经用掉多少次（给「还剩多少」这类只读用途）。没有记录就是 0。"""
    row = await session.scalar(
        LlmDailyUsage.__table__.select()
        .with_only_columns(LlmDailyUsage.calls)
        .where(
            LlmDailyUsage.user_id == user_id,
            LlmDailyUsage.day == datetime.now(UTC).date(),
        )
    )
    return int(row or 0)
