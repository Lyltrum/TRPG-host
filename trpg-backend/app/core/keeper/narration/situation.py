"""这一轮的「局面块」：把世界状态组装成喂给模型看的那段文本。

裁决与叙事**共用同一份**局面块（名单 / 世界状态 / 各能力的状态 / 阶段 / 历史 /
当前发言），区别只在**受众**：裁决拿守秘人视图（全给），分组叙事按每组的受众
重建一份。

## 🔴 为什么它是一个对象，不是一个函数

组装一次要 11 样输入，其中两样（事实账本、分段摘要）要读库。而 `narrate` 里
它被调用**三次**：裁决一次、分组叙事每组一次。做成"每次调用都传 11 个参数"
既啰嗦又容易漏，做成闭包则没法单独测、也没法跨函数传递（此前正是一个闭包，
被当成 `Callable[..., str]` 传进 `_narrate_per_audience`——签名说不清它到底
要什么）。

对象把「整轮不变的部分」和「每次调用变的部分」分开：前者是字段，后者是
`render()` 的四个参数。

## 受众裁剪在哪一层

`render(audience=...)` 会把**历史**按受众裁一遍，账本由调用方按受众算好传进来
（`ledger` 参数）。加上叙事时只喂本组听得见的原话，就是 `exec/14 P5.2d` 那句
「模型拿不到的东西才是真的漏不出来」——**这比在 prompt 末尾请它别说可靠**。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.capabilities import situation_blocks, visible_keeper_state
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.memory.chapter import Chapter, load_chapters, render_chapters, visible_chapters
from app.core.keeper.memory.fact_ledger import render_ledger, revealed_fact_ids
from app.core.keeper.memory.history import HistoryLine, visible_history
from app.core.keeper.narration.prompts import format_turn_input
from app.core.keeper.runtime.phase import format_phase_status


@dataclass(frozen=True)
class SituationBuilder:
    """整轮不变的那部分局面块。`render()` 负责每次调用变的那部分。"""

    #: 已经滤掉代码记账键的世界状态笔记。
    visible_state: dict | None
    history_lines: list[HistoryLine]
    roster: list[str]
    phase: str | None
    phase_status: str
    #: 守秘人视图的完整事实账本（分组叙事时调用方会传各组自己的那份）。
    ledger_status: str
    #: 全部分段摘要 **带受众**——`render()` 按受众裁（与 `history_lines` 同理）。
    #: 曾经是一段拼好的字符串，那时它对所有受众是同一份：分头期间地下室那段会
    #: 出现在门厅那一段的上下文里，而摘要常驻上下文，泄得比历史还久。
    chapters: list[Chapter]
    capability_blocks: list[tuple[float, str]]
    is_heartbeat: bool
    is_opening_ceremony: bool

    def render(
        self,
        *,
        audience: frozenset[str] | None,
        ledger: str,
        nickname: str,
        utterance: str,
    ) -> str:
        """按受众组装局面块（exec/14 P5.2d）。

        `audience=None` = 守秘人视图（裁决阶段用）：历史与账本全给。
        分组叙事时传该组的受众，历史/账本/原话三处一起裁。
        """
        return format_turn_input(
            self.visible_state,
            visible_history(self.history_lines, audience),
            self.roster,
            nickname,
            utterance,
            phase_status=self.phase_status,
            ledger_status=ledger,
            chapters_status=render_chapters(visible_chapters(self.chapters, audience)),
            capability_blocks=self.capability_blocks,
            is_heartbeat=self.is_heartbeat,
            is_opening_ceremony=self.is_opening_ceremony,
            phase=self.phase,
        )

    def for_keeper(self, *, nickname: str, utterance: str) -> str:
        """守秘人自己的那份（裁决阶段用）。"""
        return self.render(
            audience=None, ledger=self.ledger_status, nickname=nickname, utterance=utterance
        )


async def build_situation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    module: ScenarioModule,
    room_id: str,
    observer_id: str,
    keeper_state: dict | None,
    history_lines: list[HistoryLine],
    roster: list[str],
    players: list[tuple[str, str]],
    phase: str | None,
    ending_id: str | None,
    is_heartbeat: bool,
    is_opening_ceremony: bool,
) -> SituationBuilder:
    """读一次库，把这一轮的局面块组装器建出来。"""
    # 事实账本 L1：读全量（不设 limit）——它必须活过 HISTORY_LIMIT 的 200 条
    # 滑动窗口，这正是它存在的理由。分段摘要 L2 同理。
    async with session_factory() as db:
        known_facts = await revealed_fact_ids(db, room_id=room_id)
        chapters = await load_chapters(db, room_id=room_id)
    return SituationBuilder(
        # 代码记账的键一律不原样喂给模型，判据与"state_updates 不许写"同源。
        visible_state=visible_keeper_state(keeper_state),
        history_lines=history_lines,
        roster=roster,
        phase=phase,
        phase_status=format_phase_status(phase, ending_id),
        ledger_status=render_ledger(module, known_facts),
        chapters=chapters,
        # 已经垂直切出去的能力要摆在模型眼前的状态（exec/27 阶段 2）：能力不只
        # 要能改世界，还得让模型**看见**自己改成了什么样，否则下一轮只能从上
        # 一段散文里猜。
        capability_blocks=situation_blocks(
            module, keeper_state, observer_id=observer_id, players=tuple(players)
        ),
        is_heartbeat=is_heartbeat,
        is_opening_ceremony=is_opening_ceremony,
    )
