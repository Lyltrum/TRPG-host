"""一轮回合的运行时底座：依赖包 + 错误类型 + 各能力都要用的读写辅助。

从 `tools.py` 抽出来（exec/27 阶段 2）。原因很直接：切 `health` 时它的两个
执行函数搬进了 `capabilities/health/`，而它们要用 `KeeperDeps`、
`resolve_character`、`write_stat` 这些东西——留在 `tools.py` 就意味着
**能力要 import 那个 961 行的大杂烩**，等于把待拆的耦合原样搬进新目录。

这里只放"所有能力共用、且不属于任何一个能力"的东西。业务动词（`*_impl`）
不在这里：它们要么已经属于某个能力，要么还留在 `tools.py` 等着被切走。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.narration.contract import StatChangeNotice
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player


@dataclass
class KeeperDeps:
    """一轮回合的运行时依赖，由 KeeperAgent 构造后传给执行器/各 `*_impl`。
    room_id/player_id 从不进任何 LLM 可控的输入——LLM 伪造不了"给哪个房间
    掷骰"。"""

    room_id: str
    player_id: str  # 本轮行动的发起玩家
    session_factory: async_sessionmaker[AsyncSession]
    module: ScenarioModule
    ruleset: RulesetRead
    #: `keeper_state` 里由代码记账、`state_updates` 不许写的键。
    #:
    #: 🔴 **为什么由编排层带进来，而不是能力自己去查全局**（exec/27 阶段 3）：
    #: 它是所有能力声明的并集，只有 `capabilities/__init__` 算得出来。而
    #: `world_state` 的执行要用它——能力反过来 import 那个汇总模块，
    #: `capabilities → world_state → 汇总 → capabilities` 当场成环。
    #: **跨能力的不变量不该由能力自己去查全局，得由编排层带下来。**
    #:
    #: 故意不给默认值：默认空 = 静默失去保护，而这是一道真的闸门
    #: （漏了模型一条 state_updates 就能覆盖代码维护的记账）。
    reserved_state_keys: frozenset[str]
    # 本轮**一起发言**的全部玩家（收集窗口合并的那一批，见 service/turn_window.py）。
    # 空 = 只有发起者。`set_current_node_impl` 把这些人**以及此刻与他们同处
    # 一地的人**挪到新场景——"跟你站在一起的人跟你一起走"，见该函数 docstring
    # 里那次真人实测打脸（exec/19 #37）。
    turn_player_ids: tuple[str, ...] = ()
    rng: random.Random = field(default_factory=random.Random)
    #: 玩家自己用实体骰掷出来的出目（`exec/46` B5）。**None = 系统掷**，
    #: 那是默认行为，与本字段上线前逐字一致。
    #:
    #: 🔴 只作用于**这一次结算里属于那个玩家的那一颗骰**——对抗检定里对手那颗
    #: 仍然由系统掷（对手是 NPC，桌上没人替它掷）。
    #:
    #: 放在 deps 而不是改 `SettleHook.run` 的签名：那是「逐个列出的地方」，
    #: 改签名要动每一个 settler，而其中只有技能检定用得上它。
    manual_roll: int | None = None
    # 「读-改-写」操作（update_state/adjust_hp/san_check）的串行锁。v2 的
    # 执行器本身是顺序执行、用不上它，但保留：v1 实测过 openai-agents 会并发
    # 执行同轮工具（三次 update_state 只留最后一个键的 lost update），`*_impl`
    # 若再被并发调用方复用，这把锁就是防线。
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 本轮发生的 HP 变更（结构化，供 WS 层广播 `character.stat_changed`，
    # 供前端把角色卡的 HP 从"建卡快照"更新成实时值——真人实测 09-#4）。
    stat_changes: list[StatChangeNotice] = field(default_factory=list)


class KeeperToolError(ValueError):
    """操作参数/状态错误（找不到玩家、未知技能名等）。消息面向 LLM——
    执行器收集后作为 issues 喂给叙事阶段，让它自然圆场。"""


def _matches_player(player: Player, character: Character | None, wanted: str) -> bool:
    """房间花名册的匹配规则：昵称或角色名。

    单独抽出来是因为它有**两个**调用者——`resolve_character`（找人）和
    `is_room_member`（判别名字属于哪个命名空间）。同一条规则写两遍，迟早
    一边认得另一边不认得，而那时什么都不会变红。
    """
    return player.nickname == wanted or (character is not None and character.name == wanted)


async def is_room_member(db: AsyncSession, deps: KeeperDeps, name: str) -> bool:
    """房间里有没有这个人（按昵称或角色名）。**不抛异常。**

    给"这个名字指的是调查员还是 NPC"的判别用（`exec/20` §2.4）：两边都是
    白名单，代码自己查得出来，不该靠裁决器判对。
    """
    wanted = (name or "").strip()
    if not wanted:
        return False
    players = list(
        (await db.execute(select(Player).where(Player.room_id == deps.room_id))).scalars()
    )
    characters = list(
        (await db.execute(select(Character).where(Character.room_id == deps.room_id))).scalars()
    )
    chars_by_player = {c.player_id: c for c in characters}
    return any(_matches_player(p, chars_by_player.get(p.id), wanted) for p in players)


async def resolve_character(
    db: AsyncSession, deps: KeeperDeps, player_name: str | None
) -> tuple[Player, Character]:
    """按玩家昵称/角色名找到房间内的 (Player, Character)。不传名字 = 本轮
    行动的发起玩家。找不到时报错并列出房间里实际有谁，方便模型纠正。"""
    players = list(
        (await db.execute(select(Player).where(Player.room_id == deps.room_id))).scalars()
    )
    characters = list(
        (await db.execute(select(Character).where(Character.room_id == deps.room_id))).scalars()
    )
    chars_by_player = {c.player_id: c for c in characters}

    if player_name is None:
        player = next((p for p in players if p.id == deps.player_id), None)
    else:
        wanted = player_name.strip()
        player = next(
            (p for p in players if _matches_player(p, chars_by_player.get(p.id), wanted)),
            None,
        )
    if player is None:
        roster = "、".join(
            f"{p.nickname}（角色：{chars_by_player[p.id].name}）"
            if p.id in chars_by_player and chars_by_player[p.id].name
            else p.nickname
            for p in players
        )
        raise KeeperToolError(f"找不到玩家「{player_name}」。房间里的玩家：{roster or '（无）'}")

    character = chars_by_player.get(player.id)
    if character is None:
        raise KeeperToolError(f"玩家「{player.nickname}」还没有角色卡")
    return player, character


async def record_event(db: AsyncSession, deps: KeeperDeps, event_type: str, payload: dict) -> None:
    """工具调用留痕：写一行 events（复盘可审计守秘人的每次裁决）。"""
    db.add(
        Event(
            room_id=deps.room_id, player_id=deps.player_id, event_type=event_type, payload=payload
        )
    )
    await db.commit()


def current_stat(character: Character, key: str) -> int:
    """读衍生值的"当前值"。derived_stats 里建卡时写入的是上限，keeper 修改
    时会把原值备份成 `{key}_MAX`（见 write_stat），当前值就是 key 本身。"""
    derived: dict = character.derived_stats or {}
    value = derived.get(key)
    if not isinstance(value, int):
        raise KeeperToolError(f"角色卡缺少 {key} 数据")
    return value


def write_stat(character: Character, key: str, new_value: int) -> None:
    """写回衍生值当前值。⚠️ JSON 列必须整体重新赋值——SQLAlchemy 不追踪
    dict 的原地修改，直接 `derived[key] = x` 不会落库。"""
    derived = dict(character.derived_stats or {})
    derived.setdefault(f"{key}_MAX", derived.get(key))
    derived[key] = new_value
    character.derived_stats = derived
