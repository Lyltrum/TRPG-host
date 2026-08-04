"""progression 能力的执行层：把裁决里的阶段推进落到 `keeper_state`。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.phase import PHASE_FINISHED, PHASE_INVESTIGATION, set_phase_impl


async def execute_progression(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """结局收束优先于开场完成——一轮里两者都写时，收束是更终态的那个。"""
    report: list[str] = []
    issues: list[str] = []
    ending_reached = getattr(decision, "ending_reached", None)
    opening_complete = getattr(decision, "opening_complete", False)

    if ending_reached:
        eid = ending_reached
        if not deps.module.endings:
            # 🔴 `exec/29`：这一支原先跟"id 对得上"共用一个 else，于是**模组没有
            # 结局时任何字符串都能收束**——`endings and not any(...)` 在空列表上
            # 短路成 False，直接走进 set_phase(finished)。
            #
            # 以前撞不上是因为六个预设模组个个非空；而开放收尾的模组是合法的
            # （林中屋原文只有一句战役延续钩子），放开 endings 可以为空的同时，
            # 这条静默兜底就活了。**没有结局的模组就是收束不了，要说出来。**
            issues.append(f"结局收束未执行：本模组没有预设结局（ending_reached={eid}）")
        elif not any(e.id == eid for e in deps.module.endings):
            issues.append(f"结局收束未执行：剧本里没有 ending id={eid}")
        else:
            try:
                # 收束当轮直接 finished：叙事仍可写终章，下一行动立即拒
                report.append(await set_phase_impl(deps, PHASE_FINISHED, ending_id=eid))
            except KeeperToolError as exc:
                issues.append(f"结局收束未执行：{exc}")
    elif opening_complete:
        try:
            report.append(await set_phase_impl(deps, PHASE_INVESTIGATION))
        except KeeperToolError as exc:
            issues.append(f"开场完成未执行：{exc}")
    return report, issues
