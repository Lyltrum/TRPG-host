"""议程状态编解码（提案①）。

KEY 常量 + `load_*`（从 `keeper_state` 解析）+ `format_*`（渲染进局面块）。
写入侧在同目录的 `executor.py`（`mark_agenda_fired_impl`），那是唯一允许改
这个键的地方；`AGENDA_FIRED_KEY` 通过注册表的 `reserved_state_keys` 钩子
声明出去，`state_updates` 碰不到它。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.progress_state import AGENDA_FIRED_KEY, load_fired_agenda

# 键与解析已下沉到 `runtime/progress_state.py`（理由同 `clue_reveal/pairs.py`）。
__all__ = [
    "AGENDA_FIRED_KEY",
    "format_agenda_status",
    "load_fired_agenda",
    "render_agenda_status",
]


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


def render_agenda_status(context: SituationContext) -> str:
    """注册进局面块的 situation 钩子：从 `keeper_state` 直接渲染。

    `once` 语义和"别重复触发"是硬约束，靠模型从剧本全文里自己记等于没有——
    所以这块由代码每轮算好摆在它眼前。
    """
    return format_agenda_status(context.module, load_fired_agenda(context.keeper_state))
