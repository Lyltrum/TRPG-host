"""议程状态编解码（提案①）。

跟 `phase.py`/`visibility.py` 同一套三件套模式：KEY 常量 + `load_*`
（从 `keeper_state` 解析）+ `format_*`（渲染进裁决/叙事的局面块）。
这三个文件是 keeper_state 每类保留状态的统一范本——写入侧永远在
`tools.py` 的对应 `*_impl`（这里是 `mark_agenda_fired_impl`），是唯一
允许改 `keeper_state`/DB 的地方。
"""

from __future__ import annotations

from app.core.keeper.module_loader import ScenarioModule

AGENDA_FIRED_KEY = "已触发议程"


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


def format_agenda_status(module: ScenarioModule, fired_ids: list[str]) -> str:
    """每轮注入的议程状态：哪些还没发生、哪些已经发生过。

    为什么代码算而不是让模型从剧本全文里自己推：`once` 语义和"别重复触发"
    是硬约束，靠模型记忆等于没有。
    """
    if not module.agenda:
        return ""

    fired_set = set(fired_ids)
    pending_lines: list[str] = []
    done_lines: list[str] = []
    for event in module.agenda:
        title = event.title or "（无标题）"
        # once=False 的事件即使已触发仍留在"未发生"区——可再次发生。
        if event.id in fired_set and event.once:
            done_lines.append(f"- {event.id} · {title}")
            continue
        # 未发生区：给首句或全文，方便裁决器判断"本轮是否该触发"。
        if "。" in event.kp_text:
            kp_preview = event.kp_text.split("。", 1)[0] + "。"
        else:
            kp_preview = event.kp_text
        pending_lines.append(f"- {event.id} · {title}（{event.trigger}）：{kp_preview}")

    parts: list[str] = []
    if pending_lines:
        parts.append("### 尚未发生\n" + "\n".join(pending_lines))
    if done_lines:
        parts.append("### 已经发生\n" + "\n".join(done_lines))
    return "\n\n".join(parts)
