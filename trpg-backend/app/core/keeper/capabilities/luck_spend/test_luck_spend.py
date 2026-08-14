"""幸运消费（`exec/26 #66`，`exec/34` 第 4 步）。

硬门是纯代码，所以这里能把每一条边界钉死；`resolve` 那半要验的是**改写**——
花掉幸运不是只把 level 改成"成功"，对抗检定还得重算胜负。
"""

import random
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.luck_spend.executor import (
    _notice_payload,
    offer_luck_spend,
    resolve_luck_spend,
)
from app.core.keeper.capabilities.luck_spend.rules import OFFER_GAP_THRESHOLD, should_offer
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.pending import PendingDecision
from app.core.narration.contract import CheckResultNotice
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

_db_path = Path(tempfile.mkdtemp(prefix="trpg-luck-test-")) / "luck.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)

_STARTING_LUCK = 55


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, luck: int = _STARTING_LUCK) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code, room_name="幸运房", max_players=4, phase="InGame", keeper_state={}
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="凌铭辉")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="凌铭辉",
                occupation="记者",
                age=30,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 70,
                    "SIZ": 50,
                    "DEX": 70,
                    "APP": 50,
                    "INT": 80,
                    "POW": 50,
                    "EDU": 70,
                    "LUCK": luck,
                },  # fmt: skip
                derived_stats={"HP": 12, "MP": 10, "SAN": 50, "MOV": 8},
                skills={},
            )
        )
        await db.commit()
        return room.id, player.id


def _deps(room_id: str, player_id: str) -> KeeperDeps:
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(7),
    )


def _roll(room_id: str, player_id: str, kind: str = "skill") -> PendingDecision:
    return PendingDecision.roll(
        decision_id="req-1",
        kind=kind,
        room_id=room_id,
        player_id=player_id,
        player_nickname="凌铭辉",
        skill="侦察" if kind == "skill" else None,
        reason="翻找书桌",
    )


def _notice(
    *,
    rolled: int,
    target: int,
    level: str,
    kind: str = "skill",
    opposed: tuple[int, int, str, bool] | None = None,
) -> CheckResultNotice:
    opposed_rolled, opposed_target, opposed_level, opposed_won = opposed or (None, None, None, None)
    return CheckResultNotice(
        check_request_id="req-1",
        kind=kind,
        player_id="p1",
        skill="侦察" if kind == "skill" else None,
        rolled=rolled,
        target=target,
        level=level,
        opposed_opponent="科比特" if opposed else None,
        opposed_rolled=opposed_rolled,
        opposed_target=opposed_target,
        opposed_level=opposed_level,
        opposed_won=opposed_won,
    )


async def _luck_of(room_id: str) -> int:
    async with _session_factory() as db:
        character = (await db.execute(select(Character).filter_by(room_id=room_id))).scalar_one()
        attributes = character.attributes
        assert attributes is not None
        return int(attributes["LUCK"])


# ── 硬门 ─────────────────────────────────────────────


def test_the_gate_only_opens_for_a_plain_failure_within_reach() -> None:
    assert should_offer(kind="skill", level="失败", cost=8, luck_remaining=55)


@pytest.mark.parametrize(
    ("kind", "level", "cost", "luck", "why"),
    [
        ("san", "失败", 8, 55, "理智检定整类排除（规则）"),
        ("skill", "大失败", 8, 55, "大失败不能补（规则）"),
        ("skill", "成功", -5, 55, "成功了不用补"),
        ("skill", "失败", OFFER_GAP_THRESHOLD + 1, 55, "差太多，超出主动打扰的阈值"),
        ("skill", "失败", 8, 7, "花不起"),
    ],
)
def test_the_gate_stays_shut(kind: str, level: str, cost: int, luck: int, why: str) -> None:
    assert not should_offer(kind=kind, level=level, cost=cost, luck_remaining=luck), why


async def test_no_offer_when_the_character_has_no_luck_at_all() -> None:
    """🔴 拿不到幸运值就不问，**不猜一个默认值**——少一次 offer 可恢复，
    拿假数字算出来的花费不可恢复（禁止静默兜底）。"""
    room_id, player_id = await _seed("LK100")
    async with _session_factory() as db:
        character = (await db.execute(select(Character).filter_by(room_id=room_id))).scalar_one()
        attributes = character.attributes or {}
        character.attributes = {k: v for k, v in attributes.items() if k != "LUCK"}
        await db.commit()

    offer = await offer_luck_spend(
        _deps(room_id, player_id),
        _roll(room_id, player_id),
        _notice(rolled=68, target=60, level="失败"),
    )

    assert offer is None


async def test_an_affordable_near_miss_produces_a_card() -> None:
    room_id, player_id = await _seed("LK200")

    offer = await offer_luck_spend(
        _deps(room_id, player_id),
        _roll(room_id, player_id),
        _notice(rolled=68, target=60, level="失败"),
    )

    assert offer is not None
    assert offer.cost == 8, "花费 = 出目 − 成功率（规则书明文的换算，线性无折扣）"
    assert offer.luck_remaining == _STARTING_LUCK


# ── 答完之后 ─────────────────────────────────────────


async def _offer_for(room_id: str, player_id: str, notice: CheckResultNotice) -> PendingDecision:
    offer = await offer_luck_spend(_deps(room_id, player_id), _roll(room_id, player_id), notice)
    assert offer is not None
    return offer


async def test_declining_changes_nothing() -> None:
    room_id, player_id = await _seed("LK300")
    offer = await _offer_for(room_id, player_id, _notice(rolled=68, target=60, level="失败"))
    deps = _deps(room_id, player_id)

    _pending, notice = await resolve_luck_spend(deps, offer, accepted=False)

    assert notice.level == "失败"
    assert await _luck_of(room_id) == _STARTING_LUCK
    assert deps.check_results == []


async def test_spending_deducts_luck_and_pushes_the_result_to_a_regular_success() -> None:
    room_id, player_id = await _seed("LK400")
    offer = await _offer_for(room_id, player_id, _notice(rolled=68, target=60, level="失败"))
    deps = _deps(room_id, player_id)

    _pending, notice = await resolve_luck_spend(deps, offer, accepted=True)

    assert notice.level == "成功", "只能推成**普通**成功——它是那个换算的推论，不是独立规则"
    assert await _luck_of(room_id) == _STARTING_LUCK - 8
    assert any("消耗 8 点幸运" in line for line in deps.check_results)


async def test_the_original_roll_value_is_not_rewritten() -> None:
    """改的是**结果等级**，不是骰子。玩家看得见自己掷了 68——把它偷偷改成 60
    等于系统在说谎，而"服务端权威掷骰"这件事的全部意义就是那个数字可信。"""
    room_id, player_id = await _seed("LK500")
    offer = await _offer_for(room_id, player_id, _notice(rolled=68, target=60, level="失败"))

    _pending, notice = await resolve_luck_spend(_deps(room_id, player_id), offer, accepted=True)

    assert (notice.rolled, notice.target) == (68, 60)


async def test_spending_can_still_lose_an_opposed_check() -> None:
    """🔴 对抗检定的胜负是**比**出来的，所以花完幸运必须重算——而重算的结果
    可能还是输（对手同样成功、技能值更高）。这件事要写在卡片上，否则玩家花掉
    十几点却没赢，只会认为是 bug。"""
    room_id, player_id = await _seed("LK600")
    # 对手 50/80 普通成功、技能值 80 > 我的 60：我推成普通成功也是平级比技能值，输。
    offer = await _offer_for(
        room_id,
        player_id,
        _notice(rolled=68, target=60, level="失败", opposed=(50, 80, "成功", False)),
    )

    _pending, notice = await resolve_luck_spend(_deps(room_id, player_id), offer, accepted=True)

    assert notice.level == "成功"
    assert notice.opposed_won is False, "重算过了，而且还是输"


async def test_spending_can_win_an_opposed_check_that_was_lost() -> None:
    room_id, player_id = await _seed("LK700")
    # 对手 90/40 失败：我从失败推成成功，就赢了。
    offer = await _offer_for(
        room_id,
        player_id,
        _notice(rolled=68, target=60, level="失败", opposed=(90, 40, "失败", False)),
    )

    _pending, notice = await resolve_luck_spend(_deps(room_id, player_id), offer, accepted=True)

    assert notice.opposed_won is True


async def test_running_out_of_luck_between_the_card_and_the_answer_fails_loudly() -> None:
    """🔴 发卡之后幸运被别处扣光了：**不静默降级成"不花"**。玩家点了花、得到的
    却是没花，是最难排查的一类不一致（禁止静默兜底）。"""
    room_id, player_id = await _seed("LK800")
    offer = await _offer_for(room_id, player_id, _notice(rolled=68, target=60, level="失败"))
    async with _session_factory() as db:
        character = (await db.execute(select(Character).filter_by(room_id=room_id))).scalar_one()
        character.attributes = {**(character.attributes or {}), "LUCK": 3}
        await db.commit()

    with pytest.raises(KeeperToolError):
        await resolve_luck_spend(_deps(room_id, player_id), offer, accepted=True)


def test_the_notice_survives_a_round_trip() -> None:
    """🔴 **逐个列出的地方**：`_notice_payload` 少写一个字段，那个字段就会在玩家
    答完之后静默变回默认值——而全套测试照样绿。所以这里逐字段比。

    🔴 **2026-08-14：这条测试自己漏过一次。** 遍历 `fields()` 的写法是对的，
    但样本里新加的 `effective_rolled`/`luck_spent` 是 `None`（默认值）——
    漏传之后恢复出来**也是** `None`，两边相等，变异体大摇大摆地活了下来。
    **造的样本没走到被测分支，等于没测。**

    所以先断言样本本身「没有任何字段停在默认值上」：以后再加字段，这一步会
    先红，逼着加字段的人把样本补全，而不是让守护测试静默失效。
    """
    from dataclasses import MISSING, fields

    notice = _notice(rolled=68, target=60, level="失败", opposed=(50, 80, "成功", False))
    # 🔴 补全的这四个字段里，`san_loss`/`san_remaining` 是**旧的**——它们从来
    # 就没被这条测试真正守过，是上面那圈自检当场抓出来的。
    notice = replace(notice, san_loss=3, san_remaining=52, effective_rolled=60, luck_spent=8)

    for field in fields(CheckResultNotice):
        default = field.default if field.default is not MISSING else None
        assert getattr(notice, field.name) != default, (
            f"样本的 `{field.name}` 停在默认值上——这条守护测试对它是瞎的，先把样本补全"
        )

    restored = CheckResultNotice(**_notice_payload(notice))
    for field in fields(CheckResultNotice):
        assert getattr(restored, field.name) == getattr(notice, field.name), (
            f"`{field.name}` 没被 `_notice_payload` 带过去"
        )


async def test_no_card_for_an_ai_teammate() -> None:
    """🔴 AI 没有连接，**没有人能答这张卡**——而它挂着的时候整轮停在那儿。
    发给 AI 就是把整桌锁死（`exec/21` 第三层在检定卡片上踩过同一个坑）。"""
    room_id, player_id = await _seed("LK900")
    async with _session_factory() as db:
        player = (await db.execute(select(Player).filter_by(id=player_id))).scalar_one()
        player.is_ai = True
        await db.commit()

    offer = await offer_luck_spend(
        _deps(room_id, player_id),
        _roll(room_id, player_id),
        _notice(rolled=68, target=60, level="失败"),
    )

    assert offer is None
