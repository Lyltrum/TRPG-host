"""账号级 LLM 日配额（`app/core/llm_quota.py`）。

## 🔴 这些用例的存在理由

生产默认配额是 500，**自己开发时永远撞不到拒绝那条分支**——那正是"阈值放宽"
的代价：闸门天天在跑，但它最重要的那一半（超了会怎样）从没被执行过。
这里把配额压到 1 或 2，让拒绝路径每次跑测试都真的走一遍。

不这么做的话，这道闸门就是「整条链都在，就是没人能用到」的又一个实例：
表在、字段在、函数在，而真到了要它挡住的那天才第一次运行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core import llm_quota
from app.core.errors import ErrorCode
from app.models.user import LlmDailyUsage, User


async def _make_user(db, account: str = "quota-tester") -> User:
    user = User(account=account, password_hash="x", nickname="配额测试")
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_the_gate_lets_calls_through_until_the_quota_is_spent(db_session) -> None:
    user = await _make_user(db_session)

    assert await llm_quota.charge_one_call(user_id=user.id, quota=2) == 1
    assert await llm_quota.charge_one_call(user_id=user.id, quota=2) == 2


@pytest.mark.asyncio
async def test_the_call_after_the_last_one_is_refused(db_session) -> None:
    """🔴 拒绝那条分支——生产配额下永远跑不到，只能在这里跑。"""
    user = await _make_user(db_session)
    await llm_quota.charge_one_call(user_id=user.id, quota=1)

    with pytest.raises(llm_quota.QuotaExceeded) as excinfo:
        await llm_quota.charge_one_call(user_id=user.id, quota=1)

    # 断言的是**具体契约**，不是"它抛了个异常"：前端要靠 429 + RATE_LIMITED
    # 把"额度用完"跟"服务器坏了"分开，模糊成 500 就等于没做这个区分。
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert excinfo.value.status_code == 429
    assert excinfo.value.used == 2
    assert excinfo.value.quota == 1


@pytest.mark.asyncio
async def test_the_refused_call_is_still_counted(db_session) -> None:
    """宁可多记一次，不可少记一次。

    被拒的那一次**没有发出去**，按"花了多少钱"算它不该记。但先判后记会让两个
    并发调用同时读到 `quota - 1` 双双放行——多烧的那一次正是闸门要防的。
    这条用例把"先记再判"这个取舍钉住，免得以后有人"顺手优化"成先判。
    """
    user = await _make_user(db_session)
    await llm_quota.charge_one_call(user_id=user.id, quota=1)  # 第 1 次：放行
    with pytest.raises(llm_quota.QuotaExceeded):
        await llm_quota.charge_one_call(user_id=user.id, quota=1)  # 第 2 次：被拒

    row = await db_session.scalar(select(LlmDailyUsage).where(LlmDailyUsage.user_id == user.id))
    assert row is not None
    assert row.calls == 2


@pytest.mark.asyncio
async def test_yesterdays_usage_does_not_count_against_today(db_session) -> None:
    """跨天重置。**按 UTC 日**——服务器换时区不该让谁的额度凭空缩水。"""
    user = await _make_user(db_session)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    db_session.add(LlmDailyUsage(user_id=user.id, day=yesterday, calls=999))
    await db_session.commit()

    # 昨天烧了 999 次，今天的第一次仍然是第 1 次
    assert await llm_quota.charge_one_call(user_id=user.id, quota=5) == 1


@pytest.mark.asyncio
async def test_two_accounts_do_not_share_a_budget(db_session) -> None:
    one = await _make_user(db_session, "quota-a")
    two = await _make_user(db_session, "quota-b")

    await llm_quota.charge_one_call(user_id=one.id, quota=1)

    # 甲用完了，乙照样能用——配额主体是账号，不是全局
    assert await llm_quota.charge_one_call(user_id=two.id, quota=1) == 1


@pytest.mark.asyncio
async def test_a_call_with_no_subject_is_logged_not_silently_allowed(monkeypatch) -> None:
    """没人认领的调用**放行，但留痕**。

    放行是因为 AI 玩家、历史房间里确实存在没有账号的 player；留痕是因为它同时
    也是"某条路径忘了绑主体"的唯一症状——静默放行的话，闸门被绕过去不会有任何
    迹象，只会出现在账单上。
    """
    warnings: list[tuple[str, dict]] = []

    class _Spy:
        def warning(self, event: str, **kw: object) -> None:
            warnings.append((event, dict(kw)))

    monkeypatch.setattr(llm_quota, "logger", _Spy())

    with llm_quota.quota_subject(None):
        await llm_quota.enforce_quota(kind="adjudicate")

    assert [e for e, _ in warnings] == ["llm_call_without_quota_subject"]


@pytest.mark.asyncio
async def test_quota_zero_switches_the_gate_off_entirely(db_session, monkeypatch) -> None:
    """配 0 = 显式关闭（跑批量脚本用），不是"配额为零所以全拒"。

    这个反直觉的取值必须有测试钉住：读代码的人很容易把 0 理解成"一次都不许"，
    然后"修"成拒绝——那会让所有批量脚本在下一次部署时集体停摆。
    """
    user = await _make_user(db_session)
    monkeypatch.setattr(llm_quota, "get_settings", lambda: _settings_with(llm_daily_call_quota=0))

    with llm_quota.quota_subject(user.id):
        await llm_quota.enforce_quota(kind="adjudicate")

    # 关闭时连账都不记——不记账才是"整个摘掉"
    row = await db_session.scalar(select(LlmDailyUsage).where(LlmDailyUsage.user_id == user.id))
    assert row is None


def _settings_with(**overrides: object):
    from app.core.config import get_settings

    return get_settings().model_copy(update=overrides)


# ── 闸门真的接在 LLM 出口上了吗 ───────────────────────────────────────
#
# 🔴 上面那些用例验的是**闸门自己**好不好使，它们全绿也不能说明闸门被接上了。
# 「整条链都在，就是没人能用到」正是这个项目反复出现的缺陷形态：表在、函数在、
# 测试在，而真实调用从旁边绕过去。下面这两条验的是接线。


class _FakeInner:
    """假装是 openai 的 completions。记录自己被调了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs: object) -> object:
        self.calls += 1

        class _Resp:
            usage = None
            choices: list = []

        return _Resp()


@pytest.mark.asyncio
async def test_the_gate_is_actually_wired_into_the_llm_exit(db_session, monkeypatch) -> None:
    """配额用完之后，**请求不会发出去**。

    断言的是 `inner.calls` 没有增加——不是"抛了异常"。抛异常但请求已经发出去
    的话，钱照样花了，而这道闸门存在的唯一理由就是不让那笔钱花出去。
    """
    from app.core.llm_tape import _TapedCompletions

    user = await _make_user(db_session, "quota-wired")
    monkeypatch.setattr(llm_quota, "get_settings", lambda: _settings_with(llm_daily_call_quota=1))

    inner = _FakeInner()
    completions = _TapedCompletions(inner)

    with llm_quota.quota_subject(user.id):
        await completions.create(tape_kind="adjudicate", model="m", messages=[])
        assert inner.calls == 1

        with pytest.raises(llm_quota.QuotaExceeded):
            await completions.create(tape_kind="adjudicate", model="m", messages=[])

    assert inner.calls == 1, "额度用完之后请求仍然发出去了——闸门没拦住花钱那一步"


@pytest.mark.asyncio
async def test_replaying_a_tape_costs_nothing(db_session, monkeypatch) -> None:
    """回放不花钱，所以不许记账。

    磁带回放是**断网**跑的，记了等于让每次跑测试都消耗用户当天的额度——
    那会让配额在 CI 上莫名其妙地用完。
    """
    from app.core.llm_tape import _TapedCompletions

    user = await _make_user(db_session, "quota-replay")
    monkeypatch.setattr(llm_quota, "get_settings", lambda: _settings_with(llm_daily_call_quota=1))
    # 让回放这条路命中：_replay_or_none 返回非 None 就直接返回，不走真实调用
    monkeypatch.setattr("app.core.llm_tape._replay_or_none", lambda *a, **k: object())

    inner = _FakeInner()
    completions = _TapedCompletions(inner)

    with llm_quota.quota_subject(user.id):
        for _ in range(5):
            await completions.create(tape_kind="adjudicate", model="m", messages=[])

    assert inner.calls == 0
    row = await db_session.scalar(select(LlmDailyUsage).where(LlmDailyUsage.user_id == user.id))
    assert row is None, "回放被记进了配额——跑几次测试就能把额度耗光"
