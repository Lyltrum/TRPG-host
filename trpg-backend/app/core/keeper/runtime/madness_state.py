"""临时性疯狂：谁在疯，疯的是哪一种（共用地基）。

## 为什么它在 runtime 而不在能力目录里

判据同 `location_state.py` / `phase.py`：**共享的状态与它的读写归 runtime，
用它做裁决的字段与执行归能力。** 这里的两个写入者天生不在同一片能力里——

- **进入**由 `capabilities/san_check` 写：单次理智损失 ≥5 是它算出来的数；
- **解除**由 `capabilities/madness` 写：那是模型的判断，走裁决字段。

两片能力不许互相 import（架构测试盯着），所以"疯狂状态是什么、怎么读、
怎么写"必须落在它们共同的下游。

## 🔴 进入是代码强制，解除是裁决字段

不对称是有理由的，而且两头都是同一条判据的应用：

- 触发条件（`loss >= 5`）**是代码已经算出来的数**，症状点数也是代码掷的
  ⇒ 能确定性判断的一律代码强制，不进 prompt 请模型自觉。
- 「他缓过来了没有」是纯语义判断，代码判不了 ⇒ 只能给模型一个**字段**。
  给字段而不是"让它在叙事里写他好了"，是 `#46` 那次的教训：隐匿的解除当初
  只写在 prompt 里，于是隐匿**永不解除**。**没有 schema 字段的状态出不来。**

## 存储形态

`keeper_state` 里一个逗号分隔的字符串，`player_id@symptom_id`，跟
`PLAYER_LOCATION_KEY` 同一套。键由 `madness` 能力的 `reserved_state_keys`
声明出去，模型的 `state_updates` 改不动它。

## 症状表不在这里

在 `RulesetRead.madness_symptoms`（COC7 那份在 `app/core/coc7/content.py`）。
规则系统是插件，1D10 那张表是 COC7 的知识，不是引擎的——引擎只知道"有一张
带 id 的表、掷一次骰子从里面挑一条"。没有这张表的规则系统就没有疯狂概念，
`pick_symptom` 返回 None，没有人会进入疯狂（不伪造一个默认症状）。
"""

from __future__ import annotations

import random

import structlog

from app.core.keeper.runtime.deps import KeeperDeps, record_event
from app.dto.game import MadnessSymptomSpec, RulesetRead
from app.models.room import Room

logger = structlog.get_logger()

#: 疯狂中的调查员：`player_id@symptom_id` 的逗号串。
MADNESS_KEY = "疯狂状态"

#: 单次理智检定损失达到这个数就进入临时性疯狂（COC7）。
MADNESS_LOSS_THRESHOLD = 5


def load_madness(keeper_state: dict | None) -> dict[str, str]:
    """解析 player_id → symptom_id。保序、去空、后写覆盖先写。"""
    if not keeper_state:
        return {}
    raw = keeper_state.get(MADNESS_KEY)
    if raw is None or raw == "":
        return {}
    out: dict[str, str] = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part or "@" not in part:
            continue
        player_id, symptom_id = part.split("@", 1)
        player_id, symptom_id = player_id.strip(), symptom_id.strip()
        if player_id and symptom_id:
            out[player_id] = symptom_id
    return out


def serialize_madness(madness: dict[str, str]) -> str:
    return ", ".join(f"{pid}@{sid}" for pid, sid in madness.items())


def symptom_by_id(ruleset: RulesetRead, symptom_id: str) -> MadnessSymptomSpec | None:
    """按 id 查症状。查不到返回 None——调用方要如实表现成"查不到"，不许兜底
    成任意一条（记录里那个 id 是代码写进去的，查不到说明规则表换过了）。"""
    for spec in ruleset.madness_symptoms:
        if spec.id == symptom_id:
            return spec
    return None


def pick_symptom(ruleset: RulesetRead, rng: random.Random) -> MadnessSymptomSpec | None:
    """掷一次症状表。表为空 = 这套规则没有疯狂概念 → None。

    **按 `roll` 点数挑，不是按列表下标**：表的顺序是数据的事，1D10 落在哪一
    格是规则的事，靠下标就等于默默假设"表一定是按点数排好的十条"。点数没有
    对应条目（规则表缺格）时返回 None，而不是退回第一条。
    """
    symptoms = ruleset.madness_symptoms
    if not symptoms:
        return None
    rolled = rng.randint(1, max(spec.roll for spec in symptoms))
    for spec in symptoms:
        if spec.roll == rolled:
            return spec
    logger.warning("madness_symptom_missing", rolled=rolled)
    return None


async def enter_madness(
    deps: KeeperDeps, player_id: str, player_nickname: str
) -> MadnessSymptomSpec | None:
    """让一名调查员进入临时性疯狂，返回掷出的症状（没进入则 None）。

    两种不进入的情形，都不是兜底：
    - 这套规则没有疯狂症状表（`pick_symptom` → None）；
    - **他已经在疯狂中**——COC7 里一次发作没结束不会再叠一次，而且覆盖症状
      等于把上一条记录悄悄换掉（局面块上的那一行会变，没人知道为什么）。
    """
    symptom = pick_symptom(deps.ruleset, deps.rng)
    if symptom is None:
        return None
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return None
        state = dict(room.keeper_state or {})
        madness = load_madness(state)
        if player_id in madness:
            return None
        madness[player_id] = symptom.id
        state[MADNESS_KEY] = serialize_madness(madness)
        room.keeper_state = state
        # 留痕**也是**这里唯一的 commit（record_event 负责提交，同 agenda）。
        await record_event(
            db,
            deps,
            "keeper.madness",
            {
                "player_id": player_id,
                "player": player_nickname,
                "symptom_id": symptom.id,
                "symptom": symptom.label,
                "roll": symptom.roll,
            },
        )
    return symptom


async def clear_madness(deps: KeeperDeps, player_ids: list[str]) -> list[str]:
    """解除这些人的疯狂，返回**真的被解除**的那几个 player_id。

    返回真解除的而不是传进来的：没在疯狂中的人被写进裁决时，执行报告不该说
    "他缓过来了"——那是"写了 ≠ 变了"。
    """
    if not player_ids:
        return []
    cleared: list[str] = []
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return []
        state = dict(room.keeper_state or {})
        madness = load_madness(state)
        for player_id in player_ids:
            if madness.pop(player_id, None) is not None:
                cleared.append(player_id)
        if not cleared:
            return []
        state[MADNESS_KEY] = serialize_madness(madness)
        room.keeper_state = state
        await record_event(db, deps, "keeper.madness_recovered", {"player_ids": cleared})
    return cleared
