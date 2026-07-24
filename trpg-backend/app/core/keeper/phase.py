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
