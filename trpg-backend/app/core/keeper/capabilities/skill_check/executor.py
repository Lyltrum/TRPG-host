"""skill_check 能力的执行层：技能/属性检定的解析、掷骰与对抗结算。

共用的东西不在这里：掷骰与成功等级在 `primitives/dice.py`（san_check 也用），
技能 id 白名单在 `primitives/skills.py`，两段式待掷队列在 `keeper/pending.py`。
留在本能力里的是"这个行动该不该掷、拿哪个值掷、对手怎么比"。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.coc7.rules import evaluate_skill_base
from app.core.keeper.capabilities.skill_check.guard import (
    filter_checks_against_module,
    find_node_for_scene,
)
from app.core.keeper.contract.registry import ApplyFn, PendingContext, RolledCheck
from app.core.keeper.primitives import dice
from app.core.keeper.primitives.skills import canonical_skill_name, resolve_skill_id
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    record_event,
    resolve_character,
)
from app.core.keeper.runtime.location_state import location_of, reveal_hidden_player_impl
from app.core.keeper.runtime.pending import PendingDecision
from app.core.narration.contract import CheckResultNotice
from app.models.room import Character

logger = structlog.get_logger()

#: 潜行技能在规则表里的 id。
#:
#: 判"这次掷的是不是潜行"只能按 id 认：`PendingDecision.skill` 存的是展示名，
#: 而展示名是 `resolve_skill_target` 从 `ruleset` 里取出来的 `spec.name`，
#: 所以"用 id 反查出展示名再比较"是**闭环内**的比较，两侧都来自规则表。
#: 拿模型写的字符串直接比"潜行"才是自由文本当标识符（exec/17）。
_STEALTH_SKILL_ID = "stealth"


def _is_stealth_check(deps: KeeperDeps, display_name: str | None) -> bool:
    if display_name is None:
        return False
    return any(
        spec.id == _STEALTH_SKILL_ID and spec.name == display_name for spec in deps.ruleset.skills
    )


def resolve_skill_target(
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


async def roll_check_detail(
    deps: KeeperDeps,
    skill_name: str,
    player_name: str | None = None,
    *,
    opposed_opponent: str | None = None,
    opposed_value: int | None = None,
) -> tuple[str, dict]:
    """技能/属性检定的完整实现（掷骰 + 立刻生效），额外返回结构化明细。

    两段式玩家掷骰那条路**不走这里**，它要的是「掷完先广播、再生效」，见
    `roll_check_only` / `settle_skill_check`。这个函数留给守秘人直接掷的场合
    （`roll_check_impl`），并且是现有测试注入的接缝之一，签名与行为都不动。
    """
    text, detail, effects = await roll_check_only(
        deps,
        skill_name,
        player_name,
        opposed_opponent=opposed_opponent,
        opposed_value=opposed_value,
    )
    await effects()
    return text, detail


async def roll_check_only(
    deps: KeeperDeps,
    skill_name: str,
    player_name: str | None = None,
    *,
    opposed_opponent: str | None = None,
    opposed_value: int | None = None,
) -> tuple[str, dict, ApplyFn]:
    """**只掷骰，不生效**：返回文本、明细，以及一个「把它落到世界上」的闭包。

    掷骰这一步只读库（查角色卡拿技能值）——记事件、给叙事的那句文本都留在
    闭包里，由调用方在广播结果之后再调。为什么必须这样拆见 `RolledCheck`。

    传了 `opposed_*` 就是**对抗检定**（exec/19 #38）：对手侧同样由服务端掷骰
    （d100 对 `opposed_value`），胜负按 `dice.resolve_opposed`。
    """
    is_opposed = opposed_opponent is not None and opposed_value is not None
    async with deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
        display_name, target = resolve_skill_target(deps, character, skill_name)
    outcome = dice.evaluate_check(dice.roll_d100(deps.rng), target)
    opponent_outcome = (
        dice.evaluate_check(dice.roll_d100(deps.rng), opposed_value)
        if is_opposed and opposed_value is not None
        else None
    )
    won = dice.resolve_opposed(outcome, opponent_outcome) if opponent_outcome is not None else None
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

    if opponent_outcome is not None:
        verdict = "胜" if won else "负"
        summary = (
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
        summary = (
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

    async def effects() -> None:
        async with deps.session_factory() as db:
            await record_event(db, deps, "keeper.check", record)
        deps.check_results.append(summary)

    return text, detail, effects


async def roll_check_impl(deps: KeeperDeps, skill_name: str, player_name: str | None = None) -> str:
    text, _detail = await roll_check_detail(deps, skill_name, player_name)
    return text


async def create_pending_skill_checks(
    deps: KeeperDeps, decision: BaseModel, context: PendingContext
) -> tuple[list[PendingDecision], list[str]]:
    """把裁决里的 `checks` 解析成待掷记录——**不掷骰**。

    另：设计 02——当前场景节点若标注了 checks[]，只允许其中的 skill 进入
    check.request（第一层模组护栏，见同目录 `guard.py`）。
    """
    pending: list[PendingDecision] = []
    issues: list[str] = []
    physical_conflict = getattr(decision, "player_state", None) == "physical_conflict"

    for check in getattr(decision, "checks", ()):
        # 技能指向 id 化（exec/17）：`skill_id` 应当是白名单里的技能 id 或
        # 属性 key。JSON mode 约束不到生成，所以模型仍可能写中文名——那条
        # 路径**保留但打点**（`resolve_skill_target` 本来就同时认 id 和
        # 名字），日志能统计守规率，据此再决定要不要收紧成硬失败。
        # 不静默的意思是这里有 warning，不是"假装没发生"。
        if resolve_skill_id(deps.ruleset, check.skill_id) is None:
            logger.warning("keeper_skill_id_fallback", raw=check.skill_id)
        try:
            player, character = await resolve_character(context.db, deps, check.player)
            display_name, _target = resolve_skill_target(deps, character, check.skill_id)
        except KeeperToolError as exc:
            issues.append(f"检定[{check.skill_id}]未能发起：{exc}")
            continue
        node_id = location_of(context.keeper_state, player.id)
        # 🔴 id vs id（exec/17 (A)）：模组数据组装期已归一成规则表 id，
        # 裁决器输出的也是 id，护栏是纯集合比较——运行时不再有同义词表。
        #
        # 🔴 动手那一轮**不过护栏**（exec/19 #49，试玩实测抓到的回归）：
        # 护栏（设计 02）防的是"玩家用模组没标注的调查技能即兴挖线索"
        # （拿克苏鲁神话看穿真相那种）。**战斗不属于这个范畴**——模组不可能
        # 在每个节点都标注格斗检定点，而玩家有权动手。
        #
        # 不豁免会死循环：护栏拦掉格斗 → 本轮零检定 → #44 的兜底要求叙事
        # 停下来追问 → 玩家再说一次"我砸他的头" → 又被拦 → 又追问。
        # 试玩里连着两轮都在问"你是要砸他的头？"，玩家永远打不出这一拳。
        if not physical_conflict:
            kept, guard_issues = filter_checks_against_module(
                deps.module,
                [check.skill_id],
                current_scene=context.current_scene,
                current_node_id=node_id,
                keeper_state=context.keeper_state,
            )
            issues.extend(guard_issues)
        else:
            kept = [check.skill_id]
        if not kept:
            continue
        # 事实账本（exec/14 P4）：这名玩家所在节点上同名检定标注的
        # reveals，绑定到待掷记录上。查不到节点/查不到同名检定就是空。
        scene_node = find_node_for_scene(
            deps.module,
            context.current_scene,
            node_id=node_id,
            keeper_state=context.keeper_state,
        )
        reveals: tuple[str, ...] = ()
        if scene_node is not None:
            for module_check in scene_node.checks:
                if check.skill_id in module_check.skill_ids and module_check.reveals:
                    reveals = tuple(module_check.reveals)
                    break
        # 对抗检定（exec/19 #38）：目标值必须落在 1–100。越界就**跳过整条
        # 检定并记 issue**，不悄悄夹紧成 100——夹紧等于把裁决器写错的数字
        # 变成一个看似合理的规则结论，玩家和日志都看不出发生过什么。
        opposed_opponent: str | None = None
        opposed_value: int | None = None
        if check.opposed is not None:
            if not 1 <= check.opposed.value <= 100:
                issues.append(
                    f"对抗检定[{check.skill_id}]未发起：对手目标值 {check.opposed.value} "
                    "不在 1-100（属性点要 ×5 换算成百分位）"
                )
                continue
            opposed_opponent = check.opposed.opponent
            opposed_value = check.opposed.value
        pending.append(
            PendingDecision.roll(
                kind="skill",
                room_id=deps.room_id,
                player_id=player.id,
                player_nickname=player.nickname,
                skill=display_name,
                loss_on_success="0",
                loss_on_failure="0",
                reason=check.reason,
                reveals=reveals,
                opposed_opponent=opposed_opponent,
                opposed_value=opposed_value,
            )
        )
    return pending, issues


async def settle_skill_check(deps: KeeperDeps, pending: PendingDecision) -> RolledCheck:
    """玩家点了掷骰之后：**服务端权威**掷一次，组装成给前端的结果通知。

    骰子由 `primitives/dice` 掷，模型只消费结果、改不了点数——这是两段式玩家
    掷骰的全部意义。副作用一律留在返回的 `apply` 里（见 `RolledCheck`）。
    """
    assert pending.skill is not None
    _text, detail, effects = await roll_check_only(
        deps,
        pending.skill,
        pending.player_nickname,
        opposed_opponent=pending.opposed_opponent,
        opposed_value=pending.opposed_value,
    )
    # 🔴 exec/19 #46 的另一半：「被发现 → 解除隐匿」此前只在 prompt 里请模型
    # 自觉写回 `hidden: false`（离开地点那一半早已代码硬化）。潜行**对抗**输掉
    # 是代码判得了的——判定权就不该留在模型手里（exec/20 §2.7 给的硬化方向）。
    #
    # 只认"隐匿者本人掷潜行对抗"这一种写法：反过来写（搜索者掷侦察、把隐匿者
    # 写进 `opposed.opponent`）时，对手侧只有一个自由文本的名字，拿它去匹配
    # 玩家就是同义词打地鼠。裁决规则 4c 因此明确教了该发哪一种。
    lost_stealth = (
        pending.opposed_value is not None
        and detail.get("opposed_won") is False
        and _is_stealth_check(deps, pending.skill)
    )

    async def apply() -> None:
        await effects()
        if lost_stealth:
            revealed = await reveal_hidden_player_impl(
                deps, pending.player_id, pending.player_nickname
            )
            if revealed:
                deps.check_results.append(
                    f"{pending.player_nickname} 潜行对抗失败 → 被发现，不再隐匿"
                )

    return RolledCheck(
        notice=CheckResultNotice(
            check_request_id=pending.decision_id,
            kind="skill",
            player_id=detail["player_id"],
            skill=detail["skill"],
            rolled=detail["rolled"],
            target=detail["target"],
            level=detail["level"],
            opposed_opponent=detail.get("opposed_opponent"),
            opposed_rolled=detail.get("opposed_rolled"),
            opposed_target=detail.get("opposed_target"),
            opposed_level=detail.get("opposed_level"),
            opposed_won=detail.get("opposed_won"),
        ),
        apply=apply,
    )
