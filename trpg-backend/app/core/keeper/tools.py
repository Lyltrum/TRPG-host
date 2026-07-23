"""守秘人 agent 的六个工具（keeper agent 实验）。

分层约定：每个工具都是「普通 async 函数（`*_impl`，显式收 `KeeperDeps`）+
`@function_tool` 薄壳」两件套。业务逻辑全在 `*_impl` 里，薄壳只做
`ctx.context` 的解包——`@function_tool` 装饰后函数会变成 `FunctionTool`
对象、没法直接调用，分开写才能对掷骰分布/San 扣减这些逻辑做普通单测。

服务端权威原则：骰子由 `dice.py` 掷（LLM 只消费结果、改不了点数），
HP/San 修改真实写 `characters` 表，所有工具调用都写一行 `events` 表留痕
（复盘可审计"守秘人查了什么、掷了什么"）。

⚠️ 实验期妥协（非最终形态）：HP/San 的"当前值"直接改写 `derived_stats`
JSON（首次修改时把上限备份为 `HP_MAX`/`SAN_MAX`）——正经做法是独立的
「当前状态」存储，等实验验证过玩法再抽。
"""

import asyncio
import random
from dataclasses import dataclass, field

import structlog
from agents import RunContextWrapper, Tool, function_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.coc7_rules import evaluate_skill_base
from app.core.keeper import dice, module_loader
from app.core.keeper.module_loader import ScenarioModule
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player, Room

logger = structlog.get_logger()


@dataclass
class KeeperDeps:
    """一次 narrate 调用的运行时依赖。通过 SDK 的 RunContextWrapper 注入到
    每个工具——这些字段不进工具的 JSON Schema，LLM 看不到也伪造不了
    room_id/player_id（防止让别的房间掷骰）。"""

    room_id: str
    player_id: str  # 本轮行动的发起玩家
    session_factory: async_sessionmaker[AsyncSession]
    module: ScenarioModule
    ruleset: RulesetRead
    rng: random.Random = field(default_factory=random.Random)
    # 🔴 SDK 会**并行执行**同一条 assistant 消息里的多个工具调用。所有
    # 「读-改-写」的工具（update_state/adjust_hp/san_check）必须串行，否则
    # 并发读到同一份旧值、后提交覆盖先提交（真实 DeepSeek 冒烟实测：一轮里
    # 三次 update_state 只留下最后一个键）。房间级并发已由 action_lock 挡住，
    # 这把锁只管一次 narrate 内部的工具并发。
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class KeeperToolError(ValueError):
    """工具参数/状态错误（找不到玩家、未知技能名等）。消息面向 LLM——
    经 failure_error_function 反馈给模型，让它自己纠正参数重试。"""


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


async def roll_check_impl(deps: KeeperDeps, skill_name: str, player_name: str | None = None) -> str:
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
    return (
        f"{player.nickname} 的{display_name}检定：d100={outcome.rolled}，"
        f"目标值 {outcome.target}（困难 {outcome.target // 2}/极难 {outcome.target // 5}）"
        f"→ {outcome.level}"
    )


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
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        # ⚠️ JSON 列整体重新赋值（同 _write_stat 的原因）。
        room.keeper_state = {**(room.keeper_state or {}), key: value}
        await _record(db, deps, "keeper.state", {"key": key, "value": value})
    return f"已记录：{key} = {value}"


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
    return f"{player.nickname} HP {current} → {new_value}{status}（{reason}）"


async def san_check_impl(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> str:
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
    return (
        f"{player.nickname} 理智检定：d100={outcome.rolled}/{current} → {result}，"
        f"损失 {loss} 点（{loss_expr}），San {current} → {new_value}"
        + (f"。⚠️ {suffix}" if suffix else "")
    )


# ── @function_tool 薄壳（只做 ctx 解包，schema 由 SDK 从签名+docstring 生成） ──


def _tool_error(_ctx: RunContextWrapper[KeeperDeps], error: Exception) -> str:
    """工具失败时反馈给 LLM 的文本。KeeperToolError 的消息本来就是写给模型
    看的（含纠正建议），原样透出；其它异常只给笼统提示，细节进日志。"""
    if isinstance(error, KeeperToolError):
        return f"工具调用失败：{error}"
    return "工具内部错误，请换一种方式推进（不要重试同样的调用）。"


@function_tool(failure_error_function=_tool_error)
async def roll_check(
    ctx: RunContextWrapper[KeeperDeps], skill_name: str, player_name: str | None = None
) -> str:
    """为玩家进行一次 d100 技能/属性检定（服务端权威掷骰，结果不可更改）。

    Args:
        skill_name: COC7 技能或属性的中文名，如：侦查、图书馆使用、话术、追踪、力量、幸运。
        player_name: 要检定的玩家昵称或角色名；省略则默认本轮行动的发起玩家。
    """
    logger.info("keeper_tool", tool="roll_check", skill=skill_name, player=player_name)
    return await roll_check_impl(ctx.context, skill_name, player_name)


@function_tool(failure_error_function=_tool_error)
async def get_character_sheet(
    ctx: RunContextWrapper[KeeperDeps], player_name: str | None = None
) -> str:
    """查看玩家的角色卡（属性、衍生值、已训练技能、背景）。

    Args:
        player_name: 玩家昵称或角色名；省略则默认本轮行动的发起玩家。
    """
    logger.info("keeper_tool", tool="get_character_sheet", player=player_name)
    return await get_character_sheet_impl(ctx.context, player_name)


@function_tool(failure_error_function=_tool_error)
async def read_module(ctx: RunContextWrapper[KeeperDeps], section: str) -> str:
    """查阅模组剧本（仅守秘人可见，含剧透与真相——绝不能向玩家原样复述）。

    Args:
        section: 要查阅的部分：overview（真相/开场/KP指引）、nodes（节点列表）、
            node:<id>（某节点详情）、npcs（NPC列表）、npc:<id>（某NPC详情）、
            endings（结局列表）。
    """
    # read_module 不写 events（纯读），structlog 是它唯一的可观测痕迹——排查
    # "agent 这轮到底查没查剧本"（剧本忠实度问题）全靠这行。
    logger.info("keeper_tool", tool="read_module", section=section)
    return read_module_impl(ctx.context, section)


@function_tool(failure_error_function=_tool_error)
async def update_state(ctx: RunContextWrapper[KeeperDeps], key: str, value: str) -> str:
    """记录/更新世界状态笔记（当前场景、已揭示线索、NPC 状态、时间进度等）。
    每轮你都会在 prompt 里看到全部笔记——重要进展务必记下来，这是你唯一的
    跨轮记忆手段。

    Args:
        key: 状态项名称，如：当前场景、已获线索、游戏内时间。
        value: 状态内容（自由文本，覆盖同名旧值）。
    """
    logger.info("keeper_tool", tool="update_state", key=key)
    return await update_state_impl(ctx.context, key, value)


@function_tool(failure_error_function=_tool_error)
async def adjust_hp(
    ctx: RunContextWrapper[KeeperDeps], delta: int, reason: str, player_name: str | None = None
) -> str:
    """修改玩家的当前 HP（伤害用负数，治疗用正数）。真实写入角色卡。

    Args:
        delta: HP 变化量，伤害为负（如 -3），恢复为正。
        reason: 变化原因（会记入事件日志），如：被食尸鬼抓伤。
        player_name: 玩家昵称或角色名；省略则默认本轮行动的发起玩家。
    """
    logger.info("keeper_tool", tool="adjust_hp", delta=delta, player=player_name)
    return await adjust_hp_impl(ctx.context, delta, reason, player_name)


@function_tool(failure_error_function=_tool_error)
async def san_check(
    ctx: RunContextWrapper[KeeperDeps],
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> str:
    """为玩家进行一次理智检定（d100 对当前 San），并按结果自动扣减理智值。

    Args:
        loss_on_success: 检定成功时的损失骰子表达式，如 "0" 或 "1"。
        loss_on_failure: 检定失败时的损失骰子表达式，如 "1d6"、"1d8"。
        player_name: 玩家昵称或角色名；省略则默认本轮行动的发起玩家。
    """
    logger.info("keeper_tool", tool="san_check", player=player_name)
    return await san_check_impl(ctx.context, loss_on_success, loss_on_failure, player_name)


@function_tool(failure_error_function=_tool_error)
async def declare_no_check(ctx: RunContextWrapper[KeeperDeps], reason: str) -> str:
    """声明本轮不需要任何检定（纯对话、无风险移动、观察显而易见之物等）。

    每轮回应前你必须二选一：需要检定 → roll_check；不需要 → 调用本工具并
    说明理由。这是强制的显式裁决，不存在"什么都不调直接叙事"的路径。

    Args:
        reason: 为什么本轮不需要检定，一句话（如：纯对话，无失败可能）。
    """
    # 为什么存在这个"什么都不做"的工具：真实 DeepSeek 实测（连续三轮 prompt
    # 强化均无效）证明它的工具调用纪律拽不动——隐式调查动作全程零检定、线索
    # 白给。配合 ModelSettings(tool_choice="required")，把"要不要检定"从
    # "自愿调用"改成每轮被迫做出的显式裁决：要么掷、要么书面声明不掷，
    # 裁决和理由都进日志可审计（设计文档 02 的"发起判定显式化"落地）。
    logger.info("keeper_tool", tool="declare_no_check", reason=reason)
    return "已确认：本轮无需检定，请直接以守秘人身份叙事。"


# 显式标注 list[Tool]：Agent(tools=...) 收的是工具联合类型的列表，list 不型变，
# 推断成 list[FunctionTool] 会过不了类型检查。
KEEPER_TOOLS: list[Tool] = [
    roll_check,
    get_character_sheet,
    read_module,
    update_state,
    adjust_hp,
    san_check,
    declare_no_check,
]
