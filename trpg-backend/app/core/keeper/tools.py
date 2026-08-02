"""守秘人的游戏操作层（L3 执行）：掷骰/角色卡/剧本/状态的业务实现（keeper agent v2）。

这是 keeper_state 唯一允许写入的地方——每个 `*_impl` 对应一个"动词"
（set_phase_impl/mark_agenda_fired_impl/set_current_node_impl 等），由
`turn_executor.py`（L4 编排）根据 `KeeperDecision`（L1 契约，见 decision.py）
里哪个字段非空来调度，不再是 LLM 的自由工具——v1 的 `@function_tool` 薄壳层
已随架构推翻整体移除（自由工具调用被实测证明不可靠，见 agent.py 模块
docstring）。`*_impl` 保持普通 async 函数形态，可直接单测。

每类保留状态自己的 KEY 常量 + `load_*`/`format_*` 不在本文件——那些是
L2 状态编解码，各自有独立模块（phase.py/visibility.py/agenda_state.py/
scene_state.py），本文件只 import 它们的 KEY 常量用于写入 + 拼进
`RESERVED_STATE_KEYS`。

服务端权威原则：骰子由 `dice.py` 掷（LLM 只消费结果、改不了点数），
HP/San 修改真实写 `characters` 表，所有操作都写一行 `events` 表留痕
（复盘可审计"守秘人掷了什么、改了什么"）。

⚠️ 实验期妥协（非最终形态）：HP/San 的"当前值"直接改写 `derived_stats`
JSON（首次修改时把上限备份为 `HP_MAX`/`SAN_MAX`）——正经做法是独立的
「当前状态」存储，等实验验证过玩法再抽。
"""

import structlog
from sqlalchemy import select

from app.core.coc7_rules import evaluate_skill_base
from app.core.keeper import dice, module_loader
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    record_event,
    resolve_character,
    write_stat,
)
from app.core.keeper.location_state import (
    HIDDEN_PLAYERS_KEY,
    PLAYER_LOCATION_KEY,
    load_hidden_players,
    load_player_locations,
    location_of,
    serialize_hidden_players,
    serialize_player_locations,
)
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.primitives.npcs import resolve_npc_id
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.keeper.skill_names import canonical_skill_name
from app.models.room import Character, Player, Room

logger = structlog.get_logger()

# keeper_state 里的系统保留 key：由代码写。**唯一来源**——`state_updates` 不许
# 写它们（下面 update_state_impl），`agent` 也不把它们原样喂给模型（那边直接
# 引用这个集合，不再自己维护第二张清单：两张手维护的清单实测已经分叉过一次，
# `NPC状态` 两张都漏了，模型一条 state_updates 就能把血量记账清零）。
#
# 已经垂直切出去的能力自己声明（`reserved_state_keys` 钩子，exec/27 阶段 3）；
# 剩下的还散在各状态编解码模块（scene_state.py/phase.py/visibility.py），
# 跟着对应能力一起搬走。
RESERVED_STATE_KEYS = reserved_state_keys() | frozenset(
    {
        CURRENT_NODE_KEY,
        PLAYER_LOCATION_KEY,
        HIDDEN_PLAYERS_KEY,
    }
)


def visible_keeper_state(keeper_state: dict | None) -> dict | None:
    """喂给模型的那份世界状态笔记：滤掉所有代码记账的键。

    🔴 与 `RESERVED_STATE_KEYS`（`state_updates` 不许写）**共用同一个集合**，
    不是两张各自维护的清单。此前是两张，实测已经分叉：`NPC状态` 两张都漏了，
    于是模型既看得见那个 dict 的原始形态，又能用一条 `state_updates` 把它覆盖
    成字符串、让血量记账静默清零（exec/27 阶段 3 复查复现）。

    这些键要么是机器格式（逐人位置是 `player_id@node_id`、隐匿玩家是 player id），
    要么已经由 situation 钩子渲染成人话摆在局面块里——原样再喂一遍既是噪声也是
    泄漏。空字典/None 原样返回（"尚无记录"由渲染层表达）。
    """
    if not keeper_state:
        return keeper_state
    return {k: v for k, v in keeper_state.items() if k not in RESERVED_STATE_KEYS}


# ── 内部查询辅助 ──────────────────────────────────────


# 常见同义写法归一：规则表用的规范名 vs. 模组原文/裁决器口语化说法。
# 真人实测连续挖出三例（09-#5 的"斗殴"是复合名短名，走下面单独的后缀匹配；
# 这里是纯粹的同义词，规则表压根没有这个名字）："观察"（该轮理智检定失去
# 前置条件，检定静默丢失）、"闪躲"（"该掷躲闪了"却从未生成待掷卡片）。
# 口语说法和规则表规范名之间的落差大概率不止这几个，发现一个补一个。
# 表本身搬去 `skill_names.py`——护栏层要用同一份（exec/12 #32）。


def _resolve_skill_target(
    deps: KeeperDeps, character: Character, skill_name: str
) -> tuple[str, int]:
    """把 LLM 给的技能/属性名解析成 (规范名, 目标值)。

    支持三种写法：技能中文名（"侦查"）、技能 id（"spot-hidden"）、属性
    中文名/缩写（"力量"/"STR"/"幸运"）。技能值 = 角色卡总值，没点过的
    技能回落到基础值（含 `DEX/2` 这类公式）。
    """
    stripped = skill_name.strip()
    wanted = canonical_skill_name(stripped)
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

    # 复合名技能的短名匹配：规则表把 33 个技能存成「大类：子类」复合名
    # （如"格斗：斗殴"），但裁决器/玩家说人话只会用短名（"斗殴"）——真人实测
    # 复现过"斗殴"精确匹配失败、检定静默丢失（09-#5）。短名能唯一定位到一个
    # 复合技能时直接命中；短名本身是大类前缀（如"驾驶"对应 5 个子类）时不能
    # 瞎猜，报错列出候选，让裁决器/模型自己说清楚具体是哪一项。
    suffix_matches = [spec for spec in deps.ruleset.skills if spec.name.split("：")[-1] == wanted]
    if len(suffix_matches) == 1:
        spec = suffix_matches[0]
        value = skills.get(spec.id)
        if value is None:
            value = evaluate_skill_base(spec.base, attributes)
        return spec.name, value
    if len(suffix_matches) > 1:
        options = "、".join(spec.name for spec in suffix_matches)
        raise KeeperToolError(f"「{skill_name}」对应多个细分技能，请指定具体是哪一项：{options}")

    prefix_matches = [spec for spec in deps.ruleset.skills if spec.name.startswith(f"{wanted}：")]
    if prefix_matches:
        options = "、".join(spec.name for spec in prefix_matches)
        raise KeeperToolError(f"「{skill_name}」对应多个细分技能，请指定具体是哪一项：{options}")

    raise KeeperToolError(
        f"未知的技能/属性名「{skill_name}」。请使用 COC7 技能表中的中文名（如：侦查、"
        f"图书馆使用、话术、追踪）或属性名（如：力量、幸运）。"
    )


# ── 六个工具的业务实现（普通函数，可直接单测） ──────────────


async def roll_check_detail(
    deps: KeeperDeps,
    skill_name: str,
    player_name: str | None = None,
    *,
    opposed_opponent: str | None = None,
    opposed_value: int | None = None,
) -> tuple[str, dict]:
    """技能/属性检定的完整实现，额外返回结构化明细（两段式玩家掷骰：`check.result`
    事件需要 player_id/skill/rolled/target/level 这些字段，不能只有一段拼好的文本）。
    `roll_check_impl` 是它的薄包装，保持旧签名不破坏现有调用方/测试。

    传了 `opposed_*` 就是**对抗检定**（exec/19 #38）：对手侧同样由服务端掷骰
    （d100 对 `opposed_value`），胜负按 `dice.resolve_opposed`。两个参数都不传时
    整条路径与本功能上线前逐字一致。
    """
    is_opposed = opposed_opponent is not None and opposed_value is not None
    async with deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
        display_name, target = _resolve_skill_target(deps, character, skill_name)
        outcome = dice.evaluate_check(dice.roll_d100(deps.rng), target)
        opponent_outcome = (
            dice.evaluate_check(dice.roll_d100(deps.rng), opposed_value)
            if is_opposed and opposed_value is not None
            else None
        )
        won = (
            dice.resolve_opposed(outcome, opponent_outcome)
            if opponent_outcome is not None
            else None
        )
        record: dict = {
            "player": player.nickname,
            "skill": display_name,
            "rolled": outcome.rolled,
            "target": outcome.target,
            "level": outcome.level,
        }
        if opponent_outcome is not None:
            record["opposed"] = {
                "opponent": opposed_opponent,
                "rolled": opponent_outcome.rolled,
                "target": opponent_outcome.target,
                "level": opponent_outcome.level,
                "won": won,
            }
        await record_event(db, deps, "keeper.check", record)

    if opponent_outcome is not None:
        verdict = "胜" if won else "负"
        deps.check_results.append(
            f"{player.nickname} · {display_name}对抗{opposed_opponent}："
            f"{outcome.rolled}/{outcome.target}（{outcome.level}） vs "
            f"{opponent_outcome.rolled}/{opponent_outcome.target}"
            f"（{opponent_outcome.level}） → {verdict}"
        )
        text = (
            f"{player.nickname} 的{display_name}对抗检定（对手：{opposed_opponent}）："
            f"d100={outcome.rolled}/{outcome.target} → {outcome.level}；"
            f"对手 d100={opponent_outcome.rolled}/{opponent_outcome.target}"
            f" → {opponent_outcome.level}。{player.nickname}{verdict}。"
        )
    else:
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
    if opponent_outcome is not None:
        detail["opposed_opponent"] = opposed_opponent
        detail["opposed_rolled"] = opponent_outcome.rolled
        detail["opposed_target"] = opponent_outcome.target
        detail["opposed_level"] = opponent_outcome.level
        detail["opposed_won"] = won
    return text, detail


async def roll_check_impl(deps: KeeperDeps, skill_name: str, player_name: str | None = None) -> str:
    text, _detail = await roll_check_detail(deps, skill_name, player_name)
    return text


# 结构化背景故事（character-build-migration）8 个引导字段的中文标签。后端把
# `background_detail` 当透明存取的 opaque dict（键的含义是前端表单的事，见
# `CharacterUpdateBody.background_detail` 的字段说明），但这里是"读给 LLM 看"
# 的展示层，跟前端 `character-model.ts::BACKGROUND_DETAIL_FIELDS` 一样需要
# 人类可读的标签——两边各自维护一份，改字段要同步改这里。
_BACKGROUND_DETAIL_LABELS: dict[str, str] = {
    "personalDescription": "个人描述",
    "ideology": "信念/思想",
    "significantPeople": "重要之人",
    "meaningfulLocations": "意义非凡的地点",
    "treasuredPossessions": "珍视的物品",
    "traits": "特质",
    "injuries": "外伤",
    "phobias": "恐惧症",
}


async def get_character_sheet_impl(deps: KeeperDeps, player_name: str | None = None) -> str:
    async with deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
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

    # 结构化背景故事：只列玩家真的填过的字段，跟"已训练技能"同样的降噪原则
    # ——8 个字段建卡时可以全空，全空塞给 LLM 是纯噪音。
    detail = character.background_detail or {}
    filled = [
        f"{_BACKGROUND_DETAIL_LABELS.get(key, key)}：{value[:100]}"
        for key, value in detail.items()
        if value and value.strip()
    ]
    if filled:
        lines.append("背景细节：" + "；".join(filled))

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


#: 不挂在任何具体实体上的世界级状态（游戏内时间、天气、委托进度……）。
WORLD_SUBJECT = "world"


def resolve_state_subject(module: ScenarioModule, label: str) -> str | None:
    """把裁决器写的主体解析成剧本里的 id。解析不出返回 None。

    接受：`world`、NPC id/名字（复用 `resolve_npc_id`，含形态）、节点 id/标题。
    **全部精确匹配**——同 `resolve_npc_id` 的理由：模糊匹配是同义词打地鼠的
    开始（exec/17）。
    """
    key = (label or "").strip()
    if not key or key.casefold() == WORLD_SUBJECT:
        return WORLD_SUBJECT
    npc_id = resolve_npc_id(module, key)
    if npc_id is not None:
        return npc_id
    folded = key.casefold()
    for node in module_loader.iter_all_nodes(module.nodes):
        if node.id.casefold() == folded or node.title.casefold() == folded:
            return node.id
    return None


def _entity_name_in_key(module: ScenarioModule, key: str) -> str | None:
    """世界级键里是不是塞进了某个实体的名字（`科比特态度` 这种）。

    🔴 代码判得了触发条件，但**不阻断**——阻断会把守秘人想记的东西整条丢掉，
    而它可能只是措辞习惯。记成 issue + 日志，让"还有多少条没挂对主体"变成
    可统计的量，将来要硬化时有据可依（exec/20 的一贯做法）。
    """
    for npc in module.npcs:
        if npc.name and npc.name in key:
            return npc.id
    for node in module_loader.iter_all_nodes(module.nodes):
        if node.title and node.title in key:
            return node.id
    return None


async def update_state_impl(
    deps: KeeperDeps, key: str, value: str, subject: str = WORLD_SUBJECT
) -> tuple[str, str | None]:
    """写一条世界状态。返回 (执行报告, 问题描述或 None)。

    🔴 键的形状是 `<subject>.<key>`（世界级则只有 `key`）——见 `StateUpdate`
    的说明：没有主体的状态既不可裁剪也无法回答"谁看得见"（exec/24 §8.2）。
    """
    # write_lock：见 KeeperDeps 注释——SDK 并行工具调用下「读-改-写」必须串行。
    if key in RESERVED_STATE_KEYS:
        raise KeeperToolError(f"状态键 {key!r} 由系统记账，不能通过 state_updates 写入")
    resolved = resolve_state_subject(deps.module, subject)
    if resolved is None:
        # 未知 id 一律拒绝，与 NPC/节点/议程/密级的处理一致：白名单外的东西
        # 不进状态，否则又回到"自由文本当标识符"。
        raise KeeperToolError(
            f"未知的状态主体 {subject!r}——必须是剧本里的 NPC id / 节点 id，"
            f"或世界级状态的 {WORLD_SUBJECT!r}"
        )
    issue: str | None = None
    if resolved == WORLD_SUBJECT and (hit := _entity_name_in_key(deps.module, key)) is not None:
        issue = f"状态键 {key!r} 里带了实体名，应挂在 subject={hit!r} 上"
        logger.info(
            "keeper_state_key_should_have_subject",
            room_id=deps.room_id,
            key=key,
            suggested_subject=hit,
        )
    stored_key = key if resolved == WORLD_SUBJECT else f"{resolved}.{key}"
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        # ⚠️ JSON 列整体重新赋值（同 write_stat 的原因）。
        room.keeper_state = {**(room.keeper_state or {}), stored_key: value}
        await record_event(db, deps, "keeper.state", {"key": stored_key, "value": value})
    return f"已记录：{stored_key} = {value}", issue


def _drop_stealth_on_move(state: dict, moved_player_ids: set[str]) -> None:
    """离开原地点 → 解除隐匿（exec/19 #46）。就地改 `state`。

    exec/18 ② 早就写明"被发现、主动现身、**离开该地点**都要置回 false"，但那
    三件事此前全靠裁决器写 stealth 自觉完成。试玩实测 2026-08-01：玩家第 6 轮
    躲进街对面的阴影，之后一路垃圾场 → 温室 → 进屋 → 当面摊牌 → 地下室 →
    冲出去报警，到第 26 轮「隐匿玩家」里还挂着他——二十轮之后模型早忘了。

    三件事里**只有"离开该地点"是代码能确定性判断的**（位置变了就是变了），
    所以把这一件收归代码；"被发现/主动现身"仍留给裁决器，那是语义判断。
    这跟 #37 同一条判据：空间状态是地基，它错了投递就跟着错——多人局里一个
    永不解除的隐匿标记意味着队友**永远收不到他的消息**。
    """
    if not moved_player_ids:
        return
    hidden = load_hidden_players(state)
    remaining = hidden - moved_player_ids
    if remaining == hidden:
        return
    if remaining:
        state[HIDDEN_PLAYERS_KEY] = serialize_hidden_players(remaining)
    else:
        state.pop(HIDDEN_PLAYERS_KEY, None)


async def set_current_node_impl(deps: KeeperDeps, node_id: str) -> str:
    """写入调查员当前所在的模组场景节点 id（结构化场景指针）。

    校验 node_id 必须真实存在于剧本节点树（module.node_by_id）——拒绝模型
    编造不存在的 id，与 mark_agenda_fired_impl/mark_clues_revealed_impl
    同一套"未知 id 拒绝写入、上报为 issue"原则一致。

    P5.2 起同时写两处：房间级指针 + 逐人位置。

    ## 🔴 谁跟着走：**发言者 + 此刻与他同处一地的人**

    真人实测 2026-07-31（exec/19 #37）打脸过一次：最初只挪"本轮发言的人"，
    理由是"分头时留在别处的人不该被隔空传送走"。顾虑本身对，**默认方向选反了**
    ——绝大多数时间全队是在一起的，于是：

        第 1 轮 张家豪发言 → 张家豪@门外（凌铭辉无条目、回落房间指针，同组）
        第 2 轮 凌铭辉发言 → 凌铭辉@门口、房间指针也变门口
                            但张家豪有**显式旧条目**「门外」，不再回落 → 分头！

    两个人明明肩并肩站着，系统却判成分头：叙事分段投递、张家豪什么都收不到，
    连裁决器都读着错误的「各自所在」写下"张家豪在房子外面，未直接参与"，于是
    只给凌铭辉发了检定。一个位置默认值把三件事一起弄坏了。

    改成"跟你站在一起的人跟你一起走"：完全用现有数据算得出来，不需要模型额外
    表达，而且两头都退化正确——全队同处时全员同行；真分头时（`moves` 挪走的那
    位）不在发言者所处的地点，自然不动。
    """
    node = deps.module.node_by_id(node_id)
    if node is None:
        raise KeeperToolError(f"剧本里没有场景节点 id={node_id}")
    speakers = set(deps.turn_player_ids or (deps.player_id,))
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        # AI 玩家算进名单（exec/21 第一层）："跟你站在一起的人跟你一起走"。
        # 不算它，AI 会被永久留在原地，下一轮就被判成分头——正是 #37 那类 bug。
        roster = list(
            (await db.execute(select(Player.id).where(Player.room_id == deps.room_id))).scalars()
        )
        # 用改动**之前**的状态判断"谁跟发言者站在一起"——先写指针再判断会把
        # 所有回落到房间指针的人都算成同处，等于没判。
        speaker_places = {location_of(current_state, pid) for pid in speakers}
        movers = speakers | {
            pid for pid in roster if location_of(current_state, pid) in speaker_places
        }
        # ⚠️ 谁"真的换了地方"也必须在改指针**之前**算——写完 CURRENT_NODE_KEY
        # 再问，所有回落到房间指针的人都会显示成"已经在新节点"，等于没判。
        # （跟上面 speaker_places 同一个坑，写这段时又踩了一次。）
        moved_away = {pid for pid in movers if location_of(current_state, pid) != node_id}

        current_state[CURRENT_NODE_KEY] = node_id
        locations = load_player_locations(current_state)
        for pid in movers:
            locations[pid] = node_id
        current_state[PLAYER_LOCATION_KEY] = serialize_player_locations(locations)
        _drop_stealth_on_move(current_state, moved_away)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.node", {"node_id": node_id, "title": node.title})
    return f"当前场景节点：{node.title}（{node_id}）"


async def clear_current_node_impl(deps: KeeperDeps) -> str:
    """清空场景节点指针：人在剧本节点之外的地方（exec/19 #48）。

    房间级指针与**所有**逐人条目一起清——留着任何一条，那个人的护栏就还挂在
    旧节点上。清空后 location_of 返回 None，护栏退化到即兴层放行
    （filter_checks_against_module 找不到节点就全部放行），这是正确行为：
    剧本没写到的地方本来就没有"模组标注的检定点"可言。
    """
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        if CURRENT_NODE_KEY not in current_state and not load_player_locations(current_state):
            # 本来就没指针 = 无事发生。返回空串让调用方跳过——执行报告是
            # 喂给叙事阶段的"本轮发生了什么"，塞一条无操作进去等于噪声。
            return ""
        current_state.pop(CURRENT_NODE_KEY, None)
        current_state.pop(PLAYER_LOCATION_KEY, None)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.node", {"node_id": None, "title": None})
    return "场景已离开剧本节点范围，节点指针清空"


async def move_player_impl(deps: KeeperDeps, player_name: str, node_id: str) -> str:
    """把**一名**调查员单独挪到某个剧本节点（分头探索，P5.2）。

    与 `set_current_node_impl` 的分工是"默认 vs 覆盖"：那个写「本轮发言的人
    共同到了哪」，这个写「谁没跟着大家、单独在哪」，写的是同一张逐人表。
    一次只处理一个人，是为了让"节点 id 不存在 / 找不到这个玩家"这类问题
    退化成**这一条**的 issue，而不是整批移动一起失败。
    """
    node = deps.module.node_by_id(node_id)
    if node is None:
        raise KeeperToolError(f"剧本里没有场景节点 id={node_id}")
    async with deps.write_lock, deps.session_factory() as db:
        player, _character = await resolve_character(db, deps, player_name)
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        locations = load_player_locations(current_state)
        moved_away = {player.id} if location_of(current_state, player.id) != node_id else set()
        locations[player.id] = node_id
        current_state[PLAYER_LOCATION_KEY] = serialize_player_locations(locations)
        # 离开原地点 → 解除隐匿（exec/19 #46），与 set_current_node_impl 同口径
        _drop_stealth_on_move(current_state, moved_away)
        room.keeper_state = current_state
        await record_event(
            db,
            deps,
            "keeper.move",
            {"player": player.nickname, "node_id": node_id, "title": node.title},
        )
    return f"{player.nickname}单独前往：{node.title}（{node_id}）"


async def set_stealth_impl(deps: KeeperDeps, player_name: str, hidden: bool) -> str:
    """把一名调查员置入 / 移出隐匿状态（exec/18 ②「在场但不可见」）。

    隐匿只影响**他自己的行动被谁看见**；他照常收得到所在地点的叙事——
    "自己听得见"是这条规则的一半，另一半靠 per-observer 投递实现。
    """
    async with deps.write_lock, deps.session_factory() as db:
        player, _character = await resolve_character(db, deps, player_name)
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        hidden_ids = load_hidden_players(current_state)
        if hidden:
            hidden_ids.add(player.id)
        else:
            hidden_ids.discard(player.id)
        current_state[HIDDEN_PLAYERS_KEY] = serialize_hidden_players(hidden_ids)
        room.keeper_state = current_state
        await record_event(
            db, deps, "keeper.stealth", {"player": player.nickname, "hidden": hidden}
        )
    return f"{player.nickname}{'进入隐匿' if hidden else '不再隐匿'}"


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
        player, character = await resolve_character(db, deps, player_name)
        current = current_stat(character, "SAN")
        outcome = dice.evaluate_check(dice.roll_d100(deps.rng), current)
        loss_expr = loss_on_success if outcome.succeeded else loss_on_failure
        loss = max(0, dice.roll_dice_expr(loss_expr, deps.rng))
        new_value = max(0, current - loss)
        write_stat(character, "SAN", new_value)
        await record_event(
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
