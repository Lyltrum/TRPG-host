"""skill_check 能力的执行层：技能/属性检定的解析、掷骰与对抗结算。

共用的东西不在这里：掷骰与成功等级在 `primitives/dice.py`（san_check 也用），
技能 id 白名单在 `primitives/skills.py`，两段式待掷队列在 `keeper/pending.py`。
留在本能力里的是"这个行动该不该掷、拿哪个值掷、对手怎么比"。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.coc7.rules import evaluate_skill_base
from app.core.keeper.capabilities.skill_check.attempts import (
    CHECK_ATTEMPTS_KEY,
    load_attempts,
    record_attempt,
)
from app.core.keeper.capabilities.skill_check.guard import (
    filter_checks_against_module,
    find_node_for_scene,
)
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import PendingContext, TurnFacts
from app.core.keeper.primitives import dice
from app.core.keeper.primitives.npcs import (
    npc_ability_names,
    npc_check_target,
    npc_display_name,
    resolve_npc_ref,
)
from app.core.keeper.primitives.skills import canonical_skill_name, resolve_skill_id
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    record_event,
    resolve_character,
)
from app.core.keeper.runtime.location_state import (
    location_of,
    reveal_hidden_player_impl,
    set_stealth_impl,
)
from app.core.keeper.runtime.pending import PendingDecision
from app.core.narration.contract import CheckResultNotice
from app.models.room import Character, Room

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
    text, detail = await roll_check_only(
        deps,
        skill_name,
        player_name,
        opposed_opponent=opposed_opponent,
        opposed_value=opposed_value,
    )
    await _record_check(deps, detail)
    return text, detail


async def roll_check_only(
    deps: KeeperDeps,
    skill_name: str,
    player_name: str | None = None,
    *,
    opposed_opponent: str | None = None,
    opposed_value: int | None = None,
) -> tuple[str, dict]:
    """**只掷骰，不生效**：返回文本与结构化明细，一个字都不写。

    掷骰这一步只读库（查角色卡拿技能值）；记事件、给叙事的那句文本在
    `_record_check` 里，由调用方在广播结果之后再调。为什么必须这样拆见
    `SettleHook`。

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
    verdict = dice.opposed_verdict(outcome, opponent_outcome) if opponent_outcome else None
    if opponent_outcome is not None and verdict is not None:
        # 🔴 结论放**句首**（2026-08-14 实测）。原来的写法是"…→ 成功；对手 …
        # → 极难成功。凌铭辉负。"——「成功」两个字在前、「负」一个字在最末，
        # 叙事模型抓了前者，写成了玩家赢（代码判的是输）。**代码判对了，是这
        # 句话的结构在误导它。** 同一件事只说一次结论，且先说。
        #
        # 🔴 **2026-08-15：结论从两态改成三态。** 同一局的另一处：玩家格斗
        # 39/25 失败、米-戈 56/40 也失败，代码判 `won=False`（判对了），文本
        # 写的却是「凌铭辉负」——「负」被读成"对方赢了"，叙事于是让米-戈成功
        # 抓住玩家、造成 2 点伤害。**双方都失败在 COC 里是僵持，防守方不白赚
        # 一次攻击。** `won: bool` 装不下三种结果，那是「一份数据扮演两个角色」。
        headline = (
            f"【对抗结果：{dice.VERDICT_STALEMATE}，谁都没得手】"
            if verdict == dice.VERDICT_STALEMATE
            else f"【对抗结果：{player.nickname}{verdict}】"
        )
        text = (
            f"{headline}"
            f"{player.nickname} 的{display_name}对抗检定（对手：{opposed_opponent}）："
            f"自己 d100={outcome.rolled}/{outcome.target} → {outcome.level}；"
            f"对手 d100={opponent_outcome.rolled}/{opponent_outcome.target}"
            f" → {opponent_outcome.level}。"
            f"**以【对抗结果】为准，不要按各自的成功等级自行推断谁赢；"
            f"僵持的意思是维持现状——没有人得手，不要替任何一方写出成功的结果。**"
        )
    else:
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
        detail["opposed_verdict"] = verdict
    return text, detail


def _detail_of(pending: PendingDecision, notice: CheckResultNotice) -> dict:
    """（待决定项 + 结果通知）→ 明细。**幸运消费之后重放生效那一步靠它**：
    两样都是落过库的，而掷骰时那些局部变量早没了（`exec/34` 第 4 步）。"""
    detail: dict = {
        "player_id": notice.player_id,
        "player": pending.player_nickname,
        "skill": notice.skill,
        "rolled": notice.rolled,
        "target": notice.target,
        "level": notice.level,
    }
    if notice.opposed_opponent is not None:
        detail["opposed_opponent"] = notice.opposed_opponent
        detail["opposed_rolled"] = notice.opposed_rolled
        detail["opposed_target"] = notice.opposed_target
        detail["opposed_level"] = notice.opposed_level
        detail["opposed_won"] = notice.opposed_won
        detail["opposed_verdict"] = notice.opposed_verdict
    if notice.effective_rolled is not None:
        detail["effective_rolled"] = notice.effective_rolled
        detail["luck_spent"] = notice.luck_spent
    return detail


async def _record_check(deps: KeeperDeps, detail: dict) -> None:
    """生效的公共那一半：写 events + 给叙事留一句话。

    **只吃 detail**，不吃掷骰时的局部变量——这样它既服务于"掷完立刻生效"
    （`roll_check_detail`），也服务于"隔着玩家一个决定再生效"（幸运消费）。
    """
    record: dict = {
        "player": detail["player"],
        "skill": detail["skill"],
        "rolled": detail["rolled"],
        "target": detail["target"],
        "level": detail["level"],
    }
    if detail.get("effective_rolled") is not None:
        record["effective_rolled"] = detail["effective_rolled"]
        record["luck_spent"] = detail["luck_spent"]
    if detail.get("opposed_opponent") is not None:
        record["opposed"] = {
            "opponent": detail["opposed_opponent"],
            "rolled": detail["opposed_rolled"],
            "target": detail["opposed_target"],
            "level": detail["opposed_level"],
            "won": detail["opposed_won"],
            # 🔴 三态一路带到落库 payload：掷骰卡片按它渲染，而 `won` 装不下
            # "僵持"。缺了它，回放里僵持会显示成"负"——那是在说谎。
            "verdict": detail.get("opposed_verdict"),
        }
    async with deps.session_factory() as db:
        await record_event(db, deps, "keeper.check", record)


async def roll_check_impl(deps: KeeperDeps, skill_name: str, player_name: str | None = None) -> str:
    text, _detail = await roll_check_detail(deps, skill_name, player_name)
    return text


async def publish_stealth_check_requests(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """**只发布事实，什么都不改**：本轮为谁发起了潜行检定。

    🔴 为什么必须走 `TurnFacts` 而不是让 `movement` 直接读 `decision.checks`：
    那是一片能力伸手进另一片的字段，没有 import 所以架构测试抓不到，正是最坏
    的那种隐式耦合（同 `scene_name_declared` 的先例）。

    🔴 为什么是 order=5：`movement`（order=30）要读它，而待掷记录的创建
    （`PendingHook`）整个排在执行阶段**之后**——等不到。这个钩子唯一的职责
    就是把"裁决要求谁掷潜行"这件事提前摆到黑板上。

    解析不出玩家（编造的名字）就跳过，不记 issue：真正的 issue 由
    `create_pending_skill_checks` 在建待掷记录时统一发，这里重复发一遍会让
    同一件事在报告里出现两次。

    🔴 判"这次掷的是不是潜行"走 `resolve_skill_target` + `_is_stealth_check`
    ——跟结算侧同一套闭环内比较（两边都来自规则表），不拿模型写的字符串直接
    比"潜行"。模型写 id 还是写中文名都认得出来。
    """
    async with deps.session_factory() as db:
        for check in getattr(decision, "checks", ()):
            try:
                player, character = await resolve_character(db, deps, check.player)
                display_name, _target = resolve_skill_target(deps, character, check.skill_id)
            except KeeperToolError:
                continue
            if _is_stealth_check(deps, display_name):
                facts.stealth_check_players.add(player.id)
    return [], []


def _display_opponent(module: ScenarioModule, opponent: str) -> str:
    """对手栏是**展示用**的，玩家在掷骰卡片上看得见——填了 NPC id 就翻成名字。

    🔴 真机（2026-08-18）：`opposedOpponent = "mi-go-4"`，而同一局前几次写的是
    「米-戈 #4」。schema 里这一栏的描述是「对手是谁/什么（NPC 名、毒物、暗流……），
    展示用」，模型有时按它写、有时顺手写了 id —— 自由文本栏本来就压不住。

    **不新造机制**：NPC 掷骰那条路（`_roll_for_npc`）早就在用
    `resolve_npc_ref` + `npc_display_name`，这里接同一对。id→名字是**完全确定**
    的映射，没有语义判断。

    🔴 **认不出来就原样返回**：对手可以根本不是 NPC（毒物、暗流、一扇卡住的门），
    那时它就是一段正常的展示文本，不该被改动。
    """
    npc_id = resolve_npc_ref(module, opponent)
    if npc_id is None:
        return opponent
    return npc_display_name(module, npc_id)


async def _roll_for_npc(deps: KeeperDeps, check: object) -> list[str]:
    """名册 NPC 自己掷一次。返回 issue 列表（成功时为空）。

    ## 🔴 为什么必须有这条路

    2026-08-14 实测：叙事写「州警扣下扳机」，掷骰卡片却是
    **凌铭辉 · 射击：步枪/霰弹枪 5/42**——玩家身上根本没有枪。schema 里当时
    只有 `player`，NPC 主动做的事没有任何合法写法，模型只能记到玩家头上。

    ## 🔴 三道拒绝，一道都不能省

    1. **不在名册**（即兴造出来的州警）→ 拒。这是用户拍板的不对称：有数据卡
       才掷，没有就由叙事裁定，不让裁决器现编难度。
    2. **没说是哪一项**（`ability` 空）→ 拒。数据卡整段就在它眼前，挑一项
       不是难事；不挑就是它自己没想清楚拿什么掷。
    3. **那一项取不出百分位** → 拒，并把这个 NPC 数据卡上**有哪些项**列进
       issue。判据是那条老的：**加一道门必须同时给它配一条走得通的修法**。

    三道都是"拒绝并说出来"，不是静默跳过——静默的话，模型下一轮还会照写。
    """
    label = str(getattr(check, "npc", "") or "").strip()
    ability = str(getattr(check, "ability", "") or "").strip()
    # 解析到**形态**那一层：数据卡挂在形态上时要用它自己的数（`resolve_npc_ref`）
    npc_id = resolve_npc_ref(deps.module, label)
    if npc_id is None:
        return [
            f"NPC 检定未发起：剧本名册里没有「{label}」"
            "（即兴出来的人物没有数据卡，这一下由叙事直接裁定，不要掷骰）"
        ]
    name = npc_display_name(deps.module, npc_id)
    if not ability:
        options = npc_ability_names(deps.module, npc_id)
        return [f"NPC 检定未发起：{name} 要掷哪一项没说（数据卡上有 {options}）"]
    target = npc_check_target(deps.module, npc_id, ability)
    if target is None:
        options = npc_ability_names(deps.module, npc_id)
        return [
            f"NPC 检定未发起：{name} 的数据卡上取不出「{ability}」的百分位（可用的项：{options}）"
        ]
    outcome = dice.evaluate_check(dice.roll_d100(deps.rng), target)
    async with deps.session_factory() as db:
        await record_event(
            db,
            deps,
            "keeper.check",
            {
                # 🔴 `npc` 与 `player` 是**两个键**，不复用一个：掷骰卡片要能
                # 一眼看出这是谁掷的，而复用 `player` 正是这次 bug 的形状。
                "npc": name,
                "skill": ability,
                "rolled": outcome.rolled,
                "target": outcome.target,
                "level": outcome.level,
            },
        )
    return []


async def create_pending_skill_checks(
    deps: KeeperDeps, decision: BaseModel, context: PendingContext
) -> tuple[list[PendingDecision], list[str]]:
    """把裁决里的 `checks` 解析成待掷记录——**不掷骰**。

    另：设计 02——当前场景节点若标注了 checks[]，只允许其中的 skill 进入
    check.request（第一层模组护栏，见同目录 `guard.py`）。
    """
    pending: list[PendingDecision] = []
    issues: list[str] = []

    for check in getattr(decision, "checks", ()):
        # 🔴 **NPC 掷的那种不进待掷队列**（2026-08-15）：待掷队列的语义是
        # "等某个玩家按下掷骰"，而州警开枪不该要玩家替他按一下按钮。这一支
        # 服务端立刻掷完、直接落 `keeper.check` 事件，`pending` 一条不加。
        if getattr(check, "npc", None):
            issues.extend(await _roll_for_npc(deps, check))
            continue
        # 技能指向 id 化（exec/17）：`skill_id` 应当是白名单里的技能 id 或
        # 属性 key。JSON mode 约束不到生成，所以模型仍可能写中文名——那条
        # 路径**保留但打点**（`resolve_skill_target` 本来就同时认 id 和
        # 名字），日志能统计守规率，据此再决定要不要收紧成硬失败。
        # 不静默的意思是这里有 warning，不是"假装没发生"。
        if resolve_skill_id(deps.ruleset, check.skill_id) is None:
            logger.warning("keeper_skill_id_fallback", raw=check.skill_id)
        try:
            player, character = await resolve_character(context.db, deps, check.player)
            display_name, target_value = resolve_skill_target(deps, character, check.skill_id)
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
        # 🔴 **2026-08-15：护栏从"拦掷骰"改成"拦揭示权"。**
        #
        # 回归实测里 9 次声明检定被吞掉 6 次，全是同一个形状：玩家在
        # `forest-wandering`（模组只标了 `INT`/`LUCK`）说"追踪它/躲起来/
        # 辨方向"，`track`/`stealth`/`navigation` 逐条丢弃、**玩家侧完全静默**
        # ——没有骰子，也没有任何"这次不用掷"的提示，叙事直接给结果。
        #
        # 病根是判据选错了维度：护栏真正要防的是**"即兴掷一把就把模组真相
        # 挖出来"**，而"揭开模组事实"在代码里**本来就有独立表达**——模组检定点
        # 上的 `reveals`，绑在待掷记录上。即兴检定天生没有 `reveals`，天生
        # 揭不开任何东西。所以按技能 id 拦掷骰是**过度拦截**：它顺带没收了
        # 玩家做任何模组没预见到的动作的权利。
        #
        # 同一个模组里的另一面同样荒唐：委托人所在的 `investigation-start`
        # 标了 `fast-talk`，于是"用话术套他"掷得出来，"夸他两句让他松口"
        # （`charm`）被静默吞掉——同一个人、同一个房间、换个合理办法就没了。
        # 那张名单是导入时写死的，**不可能穷尽玩家想得到的办法**。
        #
        # 改后：**照掷，只是拿不到 reveals**（下面那段查 reveals 的代码自然
        # 得空）。防剧透一点没松，玩家的行动全部还给他。`physical_conflict`
        # 的豁免因此失去意义，一并去掉——它当初存在就是为了绕开这道拦截。
        #
        # 🔴 **护栏从此只报告，不参与执行。** 揭示权其实由下面那段绑定天然
        # 限制着：它只认"这名玩家所在节点上、标注了这个 skill_id 的检定点"，
        # 即兴检定匹配不上，自然拿不到 `reveals`。
        #
        # 这不是推理出来的——我先按"给绑定加一道 `may_reveal` 前置条件"写了
        # 一版，**变异检验里那道条件删掉测试照样绿**，说明它一行也没起作用。
        # 留着就是没有消费方的代码，删掉。护栏的返回值只用来发那条 issue。
        _kept, guard_issues = filter_checks_against_module(
            deps.module,
            [check.skill_id],
            current_scene=context.current_scene,
            current_node_id=node_id,
            keeper_state=context.keeper_state,
        )
        issues.extend(guard_issues)
        # 事实账本（exec/14 P4）：这名玩家所在节点上同名检定标注的
        # reveals，绑定到待掷记录上。查不到节点/查不到同名检定就是空。
        scene_node = find_node_for_scene(
            deps.module,
            context.current_scene,
            node_id=node_id,
            keeper_state=context.keeper_state,
        )
        reveals: tuple[str, ...] = ()
        # 🔴 **这段就是揭示权本身**：只有模组在这个节点上为这个 skill_id 标注过
        # 的检定点才交得出 reveals。即兴检定走到这里必然匹配不上 ⇒ 空。
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
            opposed_opponent = _display_opponent(deps.module, check.opposed.opponent)
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
                target=target_value,
                node=node_id,
            )
        )
    return pending, issues


async def settle_skill_check(deps: KeeperDeps, pending: PendingDecision) -> CheckResultNotice:
    """玩家点了掷骰之后：**服务端权威**掷一次，组装成给前端的结果通知。

    骰子由 `primitives/dice` 掷，模型只消费结果、改不了点数——这是两段式玩家
    掷骰的全部意义。副作用一律在 `apply_skill_check` 里（见 `SettleHook`）。
    """
    assert pending.skill is not None
    _text, detail = await roll_check_only(
        deps,
        pending.skill,
        pending.player_nickname,
        opposed_opponent=pending.opposed_opponent,
        opposed_value=pending.opposed_value,
    )
    return CheckResultNotice(
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
        opposed_verdict=detail.get("opposed_verdict"),
    )


async def _tally_attempt(
    deps: KeeperDeps, pending: PendingDecision, notice: CheckResultNotice
) -> None:
    """记一笔「这一处掷过这个技能」。判据与理由见 `attempts.py`。

    只在**站在某个地点上**时记：人在图外（指针为空）时没有地方可挂，不硬编一个。
    技能用 `pending.skill`——它是待掷记录里那个名字，跟局面块里列出来的同源。

    🔴 **地点取的是发起那一刻的，不是结算这一刻的**（2026-08-18 真机）。
    同 `reveals` 的先例：待掷期间场景会变。实测一拍之内两次力量检定——同一个
    对手、同一次拉扯——被记进了两个不同节点，因为中间那次结算叙事改了
    `当前场景节点`。记账键不稳到连「同一拍」都保不住时，架在它上面的门
    （少于 2 次不提、只列当前这一处）就全都不可靠。
    """
    from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY

    skill = (pending.skill or "").strip()
    if not skill:
        return
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return
        state = dict(room.keeper_state or {})
        # 显式降级：这个字段是 2026-08-18 加的，升级那一刻**已经在队列里**的
        # 待掷记录没有它。那时退回旧口径（结算时的位置）——精度差一点，
        # 但不至于整条记账断掉。
        here = pending.node or state.get(CURRENT_NODE_KEY)
        if not here:
            return
        state[CHECK_ATTEMPTS_KEY] = record_attempt(
            load_attempts(state), str(here), skill, succeeded=dice.is_success(notice.level)
        )
        room.keeper_state = state
        await db.commit()


async def apply_skill_check(
    deps: KeeperDeps, pending: PendingDecision, notice: CheckResultNotice
) -> None:
    """把掷出来的结果落到世界上。**输入只有落过库的那两样**，理由见 `SettleHook`。"""
    await _record_check(deps, _detail_of(pending, notice))
    await _tally_attempt(deps, pending, notice)
    # 🔴 exec/19 #46 的另一半：「被发现 → 解除隐匿」此前只在 prompt 里请模型
    # 自觉写回 `hidden: false`（离开地点那一半早已代码硬化）。潜行**对抗**输掉
    # 是代码判得了的——判定权就不该留在模型手里（exec/20 §2.7 给的硬化方向）。
    #
    # 只认"隐匿者本人掷潜行对抗"这一种写法：反过来写（搜索者掷侦察、把隐匿者
    # 写进 `opposed.opponent`）时，对手侧只有一个自由文本的名字，拿它去匹配
    # 玩家就是同义词打地鼠。裁决规则 4c 因此明确教了该发哪一种。
    #
    # 判据用的是 `notice.opposed_won`（不是掷骰当时算的那个）：花掉幸运会**重算
    # 胜负**，赢回来的人就不该被掀开隐匿。
    if (
        notice.opposed_won is False
        and pending.opposed_value is not None
        and _is_stealth_check(deps, pending.skill)
    ):
        await reveal_hidden_player_impl(deps, pending.player_id, pending.player_nickname)
        return

    # 🔴 **进入隐匿也归结算**（2026-08-15，回归实测）。
    #
    # 此前只有"输掉对抗 → 掀开"这一半是代码硬化的，而"藏进去"走的是裁决的
    # `hiding` 字段，跟掷不掷骰完全无关——实测里潜行检定被护栏吞掉、隐匿状态
    # 照样落库，**藏起来是白给的**。一条规则只写了一个方向，又一次。
    #
    # `movement` 那边看到"本轮为这个人发起了潜行检定"就不再抢先写状态
    # （`TurnFacts.stealth_check_players`），把结论留到这里按骰子给。
    #
    # 判据只认成功等级，不看对抗：普通潜行（没有明确的观察者）就是自己掷、
    # 掷过了就藏住了；有对手的那种上面已经处理完并 return。
    if _is_stealth_check(deps, pending.skill) and pending.opposed_value is None:
        if not dice.is_success(notice.level):
            return
        await set_stealth_impl(deps, pending.player_nickname, True)
