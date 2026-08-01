"""对局阶段状态机（路线第 6 步 · 提案③）。

阶段存在 `keeper_state["对局阶段"]`，不加 DB 列。合法值：

- opening：开场仪式（念 opening.script、建立委托）
- investigation：调查主循环
- ending：结局收束中
- finished：本局已结束，后续行动拒收

推进由代码写（opening_complete / ending_reached），不交给 state_updates 自由键。

## 🔴 为什么它没有跟着 `progression` 能力一起搬走（exec/27 阶段 3）

阶段值本身是**整局的生命周期状态**，编排层到处在读它：心跳要知道局是不是完了、
叙事长度按阶段给（`prose_discipline.narration_limit`）、`finished` 之后行动直接
拒收、开场轮要走仪式模式。把它塞进某一片能力，等于让 `runtime` 反向依赖那片
能力——「加一个能力不改 runtime 一行」当场破功。

判据沿用本项目对 `pending`（两段式待掷队列）的处理：**共享的流程机制归 runtime，
用它做裁决的字段与执行归能力。** 所以这里留下"阶段是什么、怎么读、怎么写"，
而"什么时候该推进"（`opening_complete` / `ending_reached` 两个裁决字段、规则 10、
结局条件的局面块）在 `capabilities/progression/`。

`format_endings_status` 跟着能力走了——它是喂给裁决器判断"该不该收束"的材料，
不是编排层要读的状态。
"""

from __future__ import annotations

from app.core.keeper.deps import KeeperDeps, KeeperToolError, record_event
from app.models.room import Room

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


async def set_phase_impl(deps: KeeperDeps, phase: str, ending_id: str | None = None) -> str:
    """写入对局阶段（及可选结局 id）。仅允许 VALID_PHASES。"""
    if phase not in VALID_PHASES:
        raise KeeperToolError(f"非法对局阶段：{phase!r}")
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        current_state[PHASE_KEY] = phase
        if ending_id:
            current_state[ENDING_ID_KEY] = ending_id
        room.keeper_state = current_state
        await record_event(
            db,
            deps,
            "keeper.phase",
            {"phase": phase, "ending_id": ending_id},
        )
    if ending_id:
        return f"对局阶段 → {phase}（结局 {ending_id}）"
    return f"对局阶段 → {phase}"
