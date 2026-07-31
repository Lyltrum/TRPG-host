"""对局阶段状态机（路线第 6 步 · 提案③）。

阶段存在 `keeper_state["对局阶段"]`，不加 DB 列。合法值：

- opening：开场仪式（念 opening.script、建立委托）
- investigation：调查主循环
- ending：结局收束中
- finished：本局已结束，后续行动拒收

推进由代码写（opening_complete / ending_reached），不交给 state_updates 自由键。
"""

from __future__ import annotations

PHASE_KEY = "对局阶段"
ENDING_ID_KEY = "结局"

PHASE_OPENING = "opening"
PHASE_INVESTIGATION = "investigation"
PHASE_ENDING = "ending"
PHASE_FINISHED = "finished"

VALID_PHASES = frozenset({PHASE_OPENING, PHASE_INVESTIGATION, PHASE_ENDING, PHASE_FINISHED})


def load_phase(keeper_state: dict | None) -> str | None:
    if not keeper_state:
        return None
    raw = keeper_state.get(PHASE_KEY)
    if raw is None or raw == "":
        return None
    value = str(raw).strip()
    return value if value in VALID_PHASES else None


def load_ending_id(keeper_state: dict | None) -> str | None:
    if not keeper_state:
        return None
    raw = keeper_state.get(ENDING_ID_KEY)
    if raw is None or raw == "":
        return None
    return str(raw).strip() or None


def format_phase_status(phase: str | None, ending_id: str | None = None) -> str:
    if phase is None:
        return ""
    labels = {
        PHASE_OPENING: "开场仪式（建立委托/递初始线索，一般不发起高风险检定）",
        PHASE_INVESTIGATION: "调查阶段",
        PHASE_ENDING: f"结局收束中（结局 id={ending_id or '—'}）",
        PHASE_FINISHED: f"本局已结束（结局 id={ending_id or '—'}）",
    }
    return labels.get(phase, phase)


def format_endings_status(module) -> str:  # noqa: ANN001 — ScenarioModule，避免循环 import
    """每轮注入的「可能的结局与触发条件」（exec/19 #47）。

    试玩实测 2026-08-01：最后一轮叙事已经完整写出了结局（警察进屋、FBI 封锁、
    烧掉房子、官方声明），而 `phase` 仍是 investigation、`ending_id` 仍是 None
    ——**故事结束了，对局没结束**，系统还在等下一轮。

    收束靠裁决器写 `ending_reached`，而结局条件此前只存在于 system prompt 里
    那份剧本全文的末尾。议程能被可靠触发，正是因为它每轮都以「议程状态」小节
    出现在局面块里、就在眼前。这里给结局同样的待遇——**把该判断的东西摆到
    它面前**，比在规则里多写一句"记得判断"可靠。

    ⚠️ 如实说：这仍是概率性改进。"这段剧情算不算命中结局"是纯语义判断，
    没有代码手段能确定性地判定它。
    """
    if not module.endings:
        return ""
    lines = []
    for ending in module.endings:
        trigger = (ending.trigger or ending.condition or "").strip()
        lines.append(f"- {ending.id} · {ending.title}：{trigger or '（未写触发条件）'}")
    return "\n".join(lines)
