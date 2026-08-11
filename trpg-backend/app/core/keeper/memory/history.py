"""历史行与受众裁剪——守秘人与 AI 玩家**共用**的那份「发生过什么」。

从 `agent.py` 抽出来是因为 exec/21 第三层：AI 玩家要按自己的受众看历史。
如果给它另写一份读法，`view(subject)` 就有了两个实现，两边迟早不一致——而
不一致的方向一定是"AI 看到了它不该看到的"（保密只会朝松的方向坏）。

这里只负责「事件 → 一行文本 + 它当时的受众」，不负责查库、不负责组 prompt。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.event import Event

# 全量重放 events 的上限条数。短模组一场 2-3 小时也就几百条，全放得下
# （DeepSeek 64K 上下文）；上限只是防御异常膨胀的房间。
HISTORY_LIMIT = 200

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
            lines.append(
                HistoryLine(
                    f"{payload.get('player', '')}进行了一次{payload.get('skill', '')}"
                    f"检定，掷出{payload.get('rolled', '?')}，目标"
                    f"{payload.get('target', '?')}，结果{payload.get('level', '')}。",
                    None,
                )
            )
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
