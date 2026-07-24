"""守秘人的游戏操作层：掷骰/角色卡/剧本/状态的业务实现（keeper agent v2）。

v2（两阶段回合制）里这些 `*_impl` 由 `decision.execute_decision`（纯代码
执行器）调用，不再是 LLM 的自由工具——v1 的 `@function_tool` 薄壳层已随
架构推翻整体移除（自由工具调用被实测证明不可靠，见 agent.py 模块 docstring）。
`*_impl` 保持普通 async 函数形态，可直接单测。

服务端权威原则：骰子由 `dice.py` 掷（LLM 只消费结果、改不了点数），
HP/San 修改真实写 `characters` 表，所有操作都写一行 `events` 表留痕
（复盘可审计"守秘人掷了什么、改了什么"）。

⚠️ 实验期妥协（非最终形态）：HP/San 的"当前值"直接改写 `derived_stats`
JSON（首次修改时把上限备份为 `HP_MAX`/`SAN_MAX`）——正经做法是独立的
「当前状态」存储，等实验验证过玩法再抽。
"""

import asyncio
import random
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.coc7_rules import evaluate_skill_base
from app.core.keeper import dice, module_loader
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.phase import (
    ENDING_ID_KEY,
    PHASE_KEY,
    VALID_PHASES,
)
from app.core.keeper.visibility import (
    ROOM_WIDE_OBSERVER,
    VISIBILITY_REVEALED_KEY,
    load_revealed_visibility,
    serialize_revealed_visibility,
)
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player, Room

logger = structlog.get_logger()

# keeper_state 里的系统保留 key：由代码写，不交给 LLM 的 state_updates。
AGENDA_FIRED_KEY = "已触发议程"

_RESERVED_STATE_KEYS = frozenset(
    {
        AGENDA_FIRED_KEY,
        VISIBILITY_REVEALED_KEY,
        PHASE_KEY,
        ENDING_ID_KEY,
    }
)


def load_fired_agenda(keeper_state: dict | None) -> list[str]:
    """从状态笔记里解析已触发的议程 id（纯函数，无 IO）。

    存储形态是逗号分隔字符串——keeper_state 的值一律是 str（update_state_impl
    的契约），不为一个列表破例。None / 缺 key / 空串 / 尾逗号都要稳健解析。
    """
    if not keeper_state:
        return []
    raw = keeper_state.get(AGENDA_FIRED_KEY)
    if raw is None or raw == "":
        return []
    # 去空白、去空项、保序（一旦写入顺序就是触发顺序，审计用得上）。
    return [part.strip() for part in str(raw).split(",") if part.strip()]


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
    rng: random.Random = field(default_factory=random.Random)
    # 「读-改-写」操作（update_state/adjust_hp/san_check）的串行锁。v2 的
    # 执行器本身是顺序执行、用不上它，但保留：v1 实测过 openai-agents 会并发
    # 执行同轮工具（三次 update_state 只留最后一个键的 lost update），`*_impl`
    # 若再被并发调用方复用，这把锁就是防线。
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 本轮的检定/理智/伤害结果记录。掷骰可见性不能依赖模型自觉——真机实测
    # 它会把数字藏进叙事（玩家掷出 94/29 失败，叙事只说"什么也没找到"，玩家
    # 以为根本没掷）。`*_impl` 往这里记，KeeperAgent.narrate 由**代码**把它们
    # 强制附加在叙事末尾广播。
    check_results: list[str] = field(default_factory=list)


class KeeperToolError(ValueError):
    """操作参数/状态错误（找不到玩家、未知技能名等）。消息面向 LLM——
    执行器收集后作为 issues 喂给叙事阶段，让它自然圆场。"""


# ── 内部查询辅助 ──────────────────────────────────────


async def _resolve_character(
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
            (
                p
                for p in players
                if p.nickname == wanted
                or (chars_by_player.get(p.id) is not None and chars_by_player[p.id].name == wanted)
            ),
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


def _resolve_skill_target(
    deps: KeeperDeps, character: Character, skill_name: str
) -> tuple[str, int]:
    """把 LLM 给的技能/属性名解析成 (规范名, 目标值)。

    支持三种写法：技能中文名（"侦查"）、技能 id（"spot-hidden"）、属性
    中文名/缩写（"力量"/"STR"/"幸运"）。技能值 = 角色卡总值，没点过的
    技能回落到基础值（含 `DEX/2` 这类公式）。
    """
    # 常见同义写法归一：技能表用「侦察」，模组文本/玩家口语常写「侦查」。
    wanted = skill_name.strip().replace("侦查", "侦察")
    attributes: dict[str, int] = character.attributes or {}
    skills: dict[str, int] = character.skills or {}

    for attr in deps.ruleset.attributes:
        if wanted in (attr.key, attr.label):
            value = attributes.get(attr.key)
            if value is None:
                raise KeeperToolError(f"角色卡缺少属性 {attr.label}")
            return attr.label, value

    for spec in deps.ruleset.skills:
        if wanted in (spec.id, spec.name) or (
            spec.name_en is not None and wanted.lower() == spec.name_en.lower()
        ):
            value = skills.get(spec.id)
            if value is None:
                value = evaluate_skill_base(spec.base, attributes)
            return spec.name, value

    raise KeeperToolError(
        f"未知的技能/属性名「{skill_name}」。请使用 COC7 技能表中的中文名（如：侦查、"
        f"图书馆使用、话术、追踪）或属性名（如：力量、幸运）。"
    )


async def _record(db: AsyncSession, deps: KeeperDeps, event_type: str, payload: dict) -> None:
    """工具调用留痕：写一行 events（复盘可审计守秘人的每次裁决）。"""
    db.add(
        Event(
            room_id=deps.room_id, player_id=deps.player_id, event_type=event_type, payload=payload
        )
    )
    await db.commit()


def _current_stat(character: Character, key: str) -> int:
    """读衍生值的"当前值"。derived_stats 里建卡时写入的是上限，keeper 修改
    时会把原值备份成 `{key}_MAX`（见 _write_stat），当前值就是 key 本身。"""
    derived: dict = character.derived_stats or {}
    value = derived.get(key)
    if not isinstance(value, int):
        raise KeeperToolError(f"角色卡缺少 {key} 数据")
    return value


def _write_stat(character: Character, key: str, new_value: int) -> None:
    """写回衍生值当前值。⚠️ JSON 列必须整体重新赋值——SQLAlchemy 不追踪
    dict 的原地修改，直接 `derived[key] = x` 不会落库。"""
    derived = dict(character.derived_stats or {})
    derived.setdefault(f"{key}_MAX", derived.get(key))
    derived[key] = new_value
    character.derived_stats = derived


# ── 六个工具的业务实现（普通函数，可直接单测） ──────────────


async def roll_check_detail(
    deps: KeeperDeps, skill_name: str, player_name: str | None = None
) -> tuple[str, dict]:
    """技能/属性检定的完整实现，额外返回结构化明细（两段式玩家掷骰：`check.result`
    事件需要 player_id/skill/rolled/target/level 这些字段，不能只有一段拼好的文本）。
    `roll_check_impl` 是它的薄包装，保持旧签名不破坏现有调用方/测试。"""
    async with deps.session_factory() as db:
        player, character = await _resolve_character(db, deps, player_name)
        display_name, target = _resolve_skill_target(deps, character, skill_name)
        outcome = dice.evaluate_check(dice.roll_d100(deps.rng), target)
        await _record(
            db,
            deps,
            "keeper.check",
            {
                "player": player.nickname,
                "skill": display_name,
                "rolled": outcome.rolled,
                "target": outcome.target,
                "level": outcome.level,
            },
        )
    deps.check_results.append(
        f"{player.nickname} · {display_name}检定："
        f"{outcome.rolled}/{outcome.target} → {outcome.level}"
    )
    text = (
        f"{player.nickname} 的{display_name}检定：d100={outcome.rolled}，"
        f"目标值 {outcome.target}（困难 {outcome.target // 2}/极难 {outcome.target // 5}）"
        f"→ {outcome.level}"
    )
    detail = {
        "player_id": player.id,
        "player": player.nickname,
        "skill": display_name,
        "rolled": outcome.rolled,
        "target": outcome.target,
        "level": outcome.level,
    }
    return text, detail


async def roll_check_impl(deps: KeeperDeps, skill_name: str, player_name: str | None = None) -> str:
    text, _detail = await roll_check_detail(deps, skill_name, player_name)
    return text


async def get_character_sheet_impl(deps: KeeperDeps, player_name: str | None = None) -> str:
    async with deps.session_factory() as db:
        player, character = await _resolve_character(db, deps, player_name)
    attributes = character.attributes or {}
    derived = character.derived_stats or {}
    skills = character.skills or {}

    # 只列玩家真实加过点的技能（总值≠基础值）——全部 80 项技能都列出来
    # 是纯噪音，基础值 agent 需要时可以让 roll_check 自己回落。
    trained: list[str] = []
    for spec in deps.ruleset.skills:
        value = skills.get(spec.id)
        if value is not None and value != evaluate_skill_base(spec.base, attributes):
            trained.append(f"{spec.name} {value}")

    lines = [
        f"玩家：{player.nickname}",
        f"角色：{character.name or '（未命名）'}（{character.occupation or '无职业'}，"
        f"{character.age or '?'} 岁，{character.gender or '?'}）",
        "属性：" + "、".join(f"{k} {v}" for k, v in attributes.items()),
        "衍生：" + "、".join(f"{k} {v}" for k, v in derived.items()),
        "已训练技能：" + ("、".join(trained) if trained else "（无，其余按基础值）"),
    ]
    if character.background:
        lines.append(f"背景：{character.background[:200]}")
    return "\n".join(lines)


def read_module_impl(deps: KeeperDeps, section: str) -> str:
    """查阅剧本（渲染与 system prompt 的剧本全文共用 module_loader 里的实现）。

    剧本全文已常驻 system prompt，这个工具是"回看细节"的补充手段——保留它
    是因为长模组未来未必能全文常驻，查询路径先留着。不碰数据库。
    """
    module = deps.module
    section = section.strip()

    if section == "overview":
        return module_loader.render_overview(module)
    if section == "nodes":
        return "调查节点列表：\n" + "\n".join(
            f"- {n.id}：{n.title}（→ {'、'.join(n.leads_to) or '终点'}）" for n in module.nodes
        )
    if section.startswith("node:"):
        node = module.node_by_id(section.removeprefix("node:"))
        if node is None:
            raise KeeperToolError(
                f"没有这个节点。可用节点：{'、'.join(n.id for n in module.nodes)}"
            )
        return module_loader.render_node(node)
    if section == "npcs":
        return "NPC 列表：\n" + "\n".join(
            f"- {n.id}：{n.name}（{n.role or ''}）" for n in module.npcs
        )
    if section.startswith("npc:"):
        npc = module.npc_by_id(section.removeprefix("npc:"))
        if npc is None:
            raise KeeperToolError(f"没有这个 NPC。可用：{'、'.join(n.id for n in module.npcs)}")
        return module_loader.render_npc(npc)
    if section == "endings":
        return module_loader.render_endings(module)

    raise KeeperToolError(
        "未知的 section。可用：overview / nodes / node:<id> / npcs / npc:<id> / endings"
    )


async def update_state_impl(deps: KeeperDeps, key: str, value: str) -> str:
    # write_lock：见 KeeperDeps 注释——SDK 并行工具调用下「读-改-写」必须串行。
    if key in _RESERVED_STATE_KEYS:
        raise KeeperToolError(f"状态键 {key!r} 由系统记账，不能通过 state_updates 写入")
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        # ⚠️ JSON 列整体重新赋值（同 _write_stat 的原因）。
        room.keeper_state = {**(room.keeper_state or {}), key: value}
        await _record(db, deps, "keeper.state", {"key": key, "value": value})
    return f"已记录：{key} = {value}"


async def mark_agenda_fired_impl(deps: KeeperDeps, event_ids: list[str]) -> str:
    """把议程事件标记为已触发（幂等：已在列表里且 once=True 的忽略）。

    once 语义必须由代码保证：LLM 的 state_updates 靠不住，实测多数轮不记。
    once=False 的事件允许重复触发——仍写事件留痕，但不重复塞进列表。

    必须走 write_lock（与 update_state_impl 一致——JSON 列是整体重新赋值，
    读改写并发会丢更新，v1 冒烟真的踩过）。
    """
    if not event_ids:
        return "议程事件触发：（无）"

    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")

        current_state = dict(room.keeper_state or {})
        already = load_fired_agenda(current_state)
        newly: list[str] = []
        report_parts: list[str] = []

        for eid in event_ids:
            event = deps.module.agenda_by_id(eid)
            title = (event.title if event is not None else None) or eid
            # once=True 且已在列表 → 幂等跳过（不是错误，只是不重复记账）
            if event is not None and event.once and eid in already:
                report_parts.append(f"{eid}（{title}，已触发过）")
                continue
            # once=False 或首次：进列表（once=False 已在列表里时不重复塞）
            if eid not in already:
                already.append(eid)
                newly.append(eid)
            report_parts.append(f"{eid}（{title}）")

        if newly:
            # ⚠️ JSON 列整体重新赋值（同 update_state_impl / _write_stat）。
            current_state[AGENDA_FIRED_KEY] = ", ".join(already)
            room.keeper_state = current_state
            await _record(db, deps, "keeper.agenda", {"event_ids": newly})
        # 纯跳过（全部已触发过）时不写库、不留痕，但返回可读报告让调用方知情。

    if not report_parts:
        return "议程事件触发：（无）"
    return "议程事件触发：" + "、".join(report_parts)


async def mark_visibility_revealed_impl(
    deps: KeeperDeps,
    pair_ids: list[str],
    *,
    room_wide: bool = True,
) -> str:
    """标记密级配对已揭开。默认房间级（@*）；幂等。"""
    if not pair_ids:
        return "密级揭开：（无）"

    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")

        current_state = dict(room.keeper_state or {})
        entries = load_revealed_visibility(current_state)
        existing = set(entries)
        newly: list[str] = []
        observer = ROOM_WIDE_OBSERVER if room_wide else deps.player_id
        report: list[str] = []

        for pid in pair_ids:
            pair = next(
                (p for p in deps.module.visibility_pairs if p.id == pid),
                None,
            )
            if pair is None:
                raise KeeperToolError(f"剧本里没有 visibility_pair id={pid}")
            key = (pid, observer)
            if key in existing or (pid, ROOM_WIDE_OBSERVER) in existing:
                report.append(f"{pid}（已揭开）")
                continue
            entries.append(key)
            existing.add(key)
            newly.append(pid)
            report.append(pid)

        if newly:
            current_state[VISIBILITY_REVEALED_KEY] = serialize_revealed_visibility(entries)
            room.keeper_state = current_state
            await _record(
                db,
                deps,
                "keeper.visibility",
                {"pair_ids": newly, "observer": observer},
            )

    return "密级揭开：" + "、".join(report) if report else "密级揭开：（无）"


async def set_phase_impl(deps: KeeperDeps, phase: str, ending_id: str | None = None) -> str:
    """写入对局阶段（及可选结局 id）。仅允许 VALID_PHASES。"""
    if phase not in VALID_PHASES:
        raise KeeperToolError(f"非法对局阶段：{phase!r}")
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        current_state[PHASE_KEY] = phase
        if ending_id:
            current_state[ENDING_ID_KEY] = ending_id
        room.keeper_state = current_state
        await _record(
            db,
            deps,
            "keeper.phase",
            {"phase": phase, "ending_id": ending_id},
        )
    if ending_id:
        return f"对局阶段 → {phase}（结局 {ending_id}）"
    return f"对局阶段 → {phase}"


async def adjust_hp_impl(
    deps: KeeperDeps, delta: int, reason: str, player_name: str | None = None
) -> str:
    # write_lock：见 KeeperDeps 注释——并行工具调用下的读-改-写必须串行。
    async with deps.write_lock, deps.session_factory() as db:
        player, character = await _resolve_character(db, deps, player_name)
        current = _current_stat(character, "HP")
        new_value = max(0, current + delta)
        _write_stat(character, "HP", new_value)
        await _record(
            db,
            deps,
            "keeper.hp",
            {"player": player.nickname, "delta": delta, "hp": new_value, "reason": reason},
        )
    status = "（已倒地/濒死）" if new_value == 0 else ""
    deps.check_results.append(f"{player.nickname} · HP {current} → {new_value}{status}")
    return f"{player.nickname} HP {current} → {new_value}{status}（{reason}）"


async def san_check_detail(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> tuple[str, dict]:
    """理智检定的完整实现，额外返回结构化明细（同 `roll_check_detail`，供
    两段式玩家掷骰的 `san.check.result` 事件使用）。`san_check_impl` 是它的
    薄包装，保持旧签名不破坏现有调用方/测试。"""
    # write_lock：见 KeeperDeps 注释——并行工具调用下的读-改-写必须串行。
    async with deps.write_lock, deps.session_factory() as db:
        player, character = await _resolve_character(db, deps, player_name)
        current = _current_stat(character, "SAN")
        outcome = dice.evaluate_check(dice.roll_d100(deps.rng), current)
        loss_expr = loss_on_success if outcome.succeeded else loss_on_failure
        loss = max(0, dice.roll_dice_expr(loss_expr, deps.rng))
        new_value = max(0, current - loss)
        _write_stat(character, "SAN", new_value)
        await _record(
            db,
            deps,
            "keeper.san",
            {
                "player": player.nickname,
                "rolled": outcome.rolled,
                "target": current,
                "succeeded": outcome.succeeded,
                "loss": loss,
                "san": new_value,
            },
        )
    result = "成功" if outcome.succeeded else "失败"
    warnings = []
    if loss >= 5:
        warnings.append("单次损失≥5，触发临时疯狂（由你按 COC7 规则叙述发作表现）")
    if new_value == 0:
        warnings.append("理智归零，角色永久疯狂")
    suffix = "；".join(warnings)
    deps.check_results.append(
        f"{player.nickname} · 理智检定：{outcome.rolled}/{current} → {result}，"
        f"San {current} → {new_value}（-{loss}）"
    )
    text = (
        f"{player.nickname} 理智检定：d100={outcome.rolled}/{current} → {result}，"
        f"损失 {loss} 点（{loss_expr}），San {current} → {new_value}"
        + (f"。⚠️ {suffix}" if suffix else "")
    )
    detail = {
        "player_id": player.id,
        "player": player.nickname,
        "rolled": outcome.rolled,
        "target": current,
        "succeeded": outcome.succeeded,
        "loss": loss,
        "san": new_value,
    }
    return text, detail


async def san_check_impl(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> str:
    text, _detail = await san_check_detail(deps, loss_on_success, loss_on_failure, player_name)
    return text
