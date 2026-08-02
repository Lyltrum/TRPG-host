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

from app.core.coc7_rules import evaluate_skill_base
from app.core.keeper import dice, module_loader
from app.core.keeper.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    record_event,
    resolve_character,
    write_stat,
)
from app.core.keeper.skill_names import canonical_skill_name
from app.models.room import Character

logger = structlog.get_logger()

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
