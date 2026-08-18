"""历史行与受众裁剪——守秘人与 AI 玩家**共用**的那份「发生过什么」。

从 `agent.py` 抽出来是因为 exec/21 第三层：AI 玩家要按自己的受众看历史。
如果给它另写一份读法，`view(subject)` 就有了两个实现，两边迟早不一致——而
不一致的方向一定是"AI 看到了它不该看到的"（保密只会朝松的方向坏）。

这里只负责「事件 → 一行文本 + 它当时的受众」，不负责查库、不负责组 prompt。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.keeper.primitives import dice
from app.models.event import Event

# 全量重放 events 的上限条数。
#
# 🔴 2026-08-16 从 200 调到 400（`exec/40` ③）。**理由是功能不是省钱**：用户
# 拍板"功能完成优先，预算优先级不高"，而记不住是长局最典型的失败形态。
# `exec/39` 实测 200 条时总输入 ~28.7k token / 64K，余量约 35k，翻倍之后仍然
# 装得下。
#
# ⚠️ 它的代价**不只是钱，还有延迟**：prompt 变长 ⇒ 裁决变慢 ⇒ 玩家每拍多等。
# 实测（500 条事件的房间，同一句话）：200 条 23,769 tok / 7,049 ms。
HISTORY_LIMIT = 400

# 历史重放里守秘人旧叙事的截断长度。不截断的话历史里全是它自己的 300-550 字
# 长篇，模型会模仿自己的旧文风越写越长（自我强化）；重要事实的长期记忆靠
# keeper_state 状态笔记承担，历史行只需要"发生过什么"的梗概。
HISTORY_NARRATION_CLIP = 160

# 事件类型 → 历史行格式化器。keeper.state 不进历史（状态笔记单独整体注入），
# 工具留痕（检定/HP/San）进历史是为了让守秘人记得自己此前的裁决结果。
EVENT_LABELS = {
    "keeper.check": "检定",
    "keeper.san": "理智",
    "keeper.hp": "生命",
}

#: 会被重放成历史行的事件类型。查库的 `IN (...)` 与下面的解析分支必须同源，
#: 否则会出现"查出来了但没人认识、静默丢弃"。
HISTORY_EVENT_TYPES = ["action.submit", "narration.push", *EVENT_LABELS.keys()]


@dataclass(frozen=True, slots=True)
class HistoryLine:
    """一条历史行 + 它当时的受众（exec/14 P5.2d）。

    `audience=None` = 公开，全房间都经历过（未分头时的常态，也是 P5.2d 之前
    所有老数据的形态）。分头/隐匿/私密时只有那几个人经历过。
    """

    text: str
    audience: frozenset[str] | None = None


#: 对抗三态 → 给叙事看的一句话。
#:
#: 🔴 **只给结论，不给两边的出目与成功等级**（2026-08-18）。这不是省事，
#: 是「保密靠拿不到，不是请你别说」：两次真机写反的形态一模一样——玩家自己
#: 成功、对手成功等级更高 ⇒ 判负，而叙事眼里那句「结果**成功**」压过了一切，
#: 于是把输写成了赢。数字对后续叙事没有价值（复述骰子本来就不该），而它是
#: 已证实的误导源。玩家侧的可见性由 `check.result` 这条 WS 事件保证，
#: 前端卡片照旧显示双方的出目——两件事分属两个出口，不要混。
_OPPOSED_LINE = {
    dice.VERDICT_WIN: "{player}在{skill}对抗中赢了{opponent}。",
    dice.VERDICT_LOSE: "{player}在{skill}对抗中输给了{opponent}——{opponent}压过了他。",
    dice.VERDICT_STALEMATE: "{player}和{opponent}的{skill}对抗僵持不下，谁都没得手，维持原状。",
}


def _render_check(payload: dict) -> str:
    """一次检定在历史窗口里的样子。

    🔴 **对抗必须渲染结论**（2026-08-18 真机，同一个 bug 的第二、三次）。
    此前这里只渲染玩家自己那一半（`rolled`/`target`/`level`），`opposed` 整块
    ——包括 `verdict`——**一个字都没进去**。而 `settle_skill_check` 里那段精心
    组装的三态文本（结论提句首 + 明写"不要按成功等级自行推断"）写进的是
    `deps.check_results`——**那个字段从头到尾没有任何读取方**（已于同日删除：
    它是 07-23 加的"掷骰可见性硬保证"的数据源，07-28 那个职责搬到结构化 WS
    事件之后，读取方被删、容器留了下来）。

    ⇒ 两跑写反**不是模型不遵守，是对抗结论根本没送到它眼前**。`exec/20 §1.20`
    当时判成"prompt 手段已用尽、只能靠状态化硬化"，那是**拿错了度量对象**：
    量的是"代码组装了什么"，不是"叙事真正收到了什么"。
    """
    # 🔴 `player` 与 `npc` 是**两个键**（`_record_check`：NPC 掷的骰不许挂在任何
    # 玩家名下）。只读 `player` 的话，NPC 那条在历史里会渲染成「进行了一次拍击
    # 检定」——**主语没了**，而这正是 `#79` 当初要防的"州警开枪被记成玩家射击"。
    # 2026-08-18 删 `deps.check_results` 死链时才发现新载体漏了这一半。
    who = payload.get("player") or payload.get("npc") or ""
    skill = payload.get("skill", "")
    opposed = payload.get("opposed")
    if isinstance(opposed, dict):
        template = _OPPOSED_LINE.get(str(opposed.get("verdict")))
        if template is not None:
            return template.format(
                player=who, skill=skill, opponent=opposed.get("opponent", "对手")
            )
    return (
        f"{who}进行了一次{skill}检定，掷出{payload.get('rolled', '?')}，"
        f"目标{payload.get('target', '?')}，结果{payload.get('level', '')}。"
    )


def is_visible_to(recorded: frozenset[str] | None, viewer: frozenset[str] | None) -> bool:
    """这条**当时受众是 `recorded`** 的东西，`viewer` 这组人看不看得见。

    抽出来是因为 L2 分段摘要（`memory/chapter.py`）要用**同一条**判据：
    同一条规则写两遍，迟早一边认得另一边不认得，而不一致的方向一定是
    "有人看到了不该看到的"（保密只会朝松的方向坏）。

    - `viewer is None` = 守秘人视图，全给。
    - `recorded is None` = 公开，谁都看得见。
    - 否则要求 `viewer ⊆ recorded`：**viewer 里每个人当时都在场**才算看得见。

    空 `viewer`（"谁都不是"）由调用方显式挡在前面——`frozenset() <= x` 恒为真，
    在这里放行就等于把私密行发给"没有人"，见 `visible_history` 里那段注释。
    """
    if viewer is None:
        return True
    if recorded is None:
        return True
    return bool(viewer) and viewer <= recorded


def visible_history(lines: list[HistoryLine], audience: frozenset[str] | None) -> list[str]:
    """裁剪出这组观察者**共同经历过**的历史（exec/14 P5.2d）。

    `audience=None` = 守秘人视图，全给（它对整局一致性负责，必须看见全部）。

    `audience=frozenset()`（空集）= 没有人，只给公开行（见下面那段注释）。

    否则判据是**交集**、朝保密方向失败：只有当 `audience` 里每个人当时都在场，
    这一行才进他们那一段的上下文。这是 P5.2 从"提示词请你别说"升级成"根本
    不知道"的关键一步——门厅那段的模型看不到地下室的历史，就漏不出来。
    """
    if audience is None:
        return [line.text for line in lines]
    if not audience:
        # 🔴 空受众 = 没有人。**必须显式挡住**：`frozenset() <= x` 恒为真，
        # 不挡的话"谁都不是"会拿到**全部**历史（含私密行）——朝泄密方向失败，
        # 跟上面那句"朝保密方向失败"正相反。
        #
        # 目前不可达（分组来自 `group_players`，恒非空），但可达性是调用方的
        # 性质，不是本函数的保证。发现于 exec/27 阶段 4 的变异检验：那个变异体
        # 本身是无效的（改成空集不改行为），**恰恰因为不改行为才暴露了这里**。
        return [line.text for line in lines if line.audience is None]
    return [line.text for line in lines if is_visible_to(line.audience, audience)]


def history_lines_from_events(events: list[Event], nicknames: dict[str, str]) -> list[HistoryLine]:
    """把按时间正序排好的事件行渲染成历史行。

    `nicknames`：player_id → 昵称。查不到时退回"玩家"（成员退房本期不存在，
    这只是防御）。
    """
    lines: list[HistoryLine] = []
    for event in events:
        payload = event.payload or {}
        # 受众：payload 里带 `audience` 的事件只有那几个人经历过（分头/隐匿/
        # 私密时写入，见 ws.py）。没有这个字段 = 公开，老数据天然如此。
        raw_audience = payload.get("audience")
        audience = frozenset(str(x) for x in raw_audience) if raw_audience else None
        if event.event_type == "action.submit":
            who = nicknames.get(event.player_id or "", "玩家")
            lines.append(HistoryLine(f"{who}：{payload.get('utterance', '')}", audience))
        elif event.event_type == "narration.push":
            text = payload.get("text", "")
            if len(text) > HISTORY_NARRATION_CLIP:
                text = text[:HISTORY_NARRATION_CLIP] + "……"
            lines.append(HistoryLine(f"守秘人：{text}", audience))
        elif event.event_type == "keeper.check":
            # 🔴 2026-07-30（exec/11 待办2）：曾用 `[检定] 玩家 技能：a/b → 结果`
            # 这种方括号"记账行"格式喂给叙事 LLM 看历史，真人实测复现过
            # 叙事正文里编造出一句格式几乎一样但数值全假的"记账"（见
            # prose_discipline.py 的 _FAKE_STAT_LOG_LEAK）——模型照猫画虎
            # 模仿了这里看到的模板。改成普通叙述句，不留可逐字复刻的模板。
            # ⑦⑧ 定稿：检定过程与结果、HP/SAN 一律公开 → 受众恒为 None
            lines.append(HistoryLine(_render_check(payload), None))
        elif event.event_type == "keeper.san":
            lines.append(
                HistoryLine(
                    f"{payload.get('player', '')}遭受理智冲击，损失"
                    f"{payload.get('loss', '?')}点理智，当前理智值{payload.get('san', '?')}。",
                    None,
                )
            )
        elif event.event_type == "keeper.hp":
            lines.append(
                HistoryLine(
                    f"{payload.get('player', '')}的生命值发生变化："
                    f"{payload.get('delta', '?')}点（{payload.get('reason', '')}），"
                    f"当前生命值{payload.get('hp', '?')}。",
                    None,
                )
            )
    return lines
