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
from app.core.keeper.context_budget import log_turn_input
from app.core.keeper.contract.module_loader import ScenarioModule, render_recall
from app.core.keeper.memory.chapter import Chapter, load_chapters, render_chapters, visible_chapters
from app.core.keeper.memory.fact_ledger import render_ledger, revealed_fact_ids
from app.core.keeper.memory.history import HistoryLine, visible_history
from app.core.keeper.narration.party_sheet import format_party_sheet, load_party_characters
from app.core.keeper.narration.prompts import format_turn_input
from app.core.keeper.runtime.focus import focus_set, ids_mentioned_by, should_layer
from app.core.keeper.runtime.pending import MERGE_CONFIRM_KIND, pending_decision_manager
from app.core.keeper.runtime.phase import format_phase_status
from app.dto.game import RulesetRead


@dataclass(frozen=True)
class SituationBuilder:
    """整轮不变的那部分局面块。`render()` 负责每次调用变的那部分。"""

    #: 只给上下文预算观测用（`keeper/context_budget.py`）——组装本身不读它。
    room_id: str
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
    #: 叙事器那份能力块——声明了 `keeper_only` 的不在里面（`exec/23 #77`）。
    narrator_capability_blocks: list[tuple[float, str]]
    is_heartbeat: bool
    is_opening_ceremony: bool
    #: 「这些调查员会什么」（见 `party_sheet.py`）。**只给裁决那一拍**——叙事器
    #: 不需要技能数值，给了反而诱它把数字写进散文。
    #:
    #: 默认空串让既有构造一个字都不用改（同 `SituationContext` 加字段的做法）。
    #: 🔴 默认值意味着"忘了传"跟"这局没有卡"长得一样，所以 `build_situation`
    #: 确实填了它这件事**由 test_party_sheet.py 单独钉住**——否则就是那条老毛病：
    #: 加了字段没有消费方，两头都不会变红。
    party_sheet: str = ""
    #: 分层注入用的两样（`exec/47` P1b）。**短模组两个都是 None**，此时
    #: `render()` 里那一段整块跳过，局面块与分层之前逐字节一致。
    #:
    #: 🔴 这里存的是**原始** `keeper_state`，不是上面那份 `visible_state`——
    #: 关注集要读 `当前场景节点` / `在场NPC` / `玩家位置` 这些**保留键**，
    #: 而 `visible_keeper_state` 恰恰把它们滤掉了。拿错那一份的表现是
    #: 「召回集永远为空」，而且不会报错。
    module: ScenarioModule | None = None
    raw_state: dict | None = None

    def render(
        self,
        *,
        audience: frozenset[str] | None,
        ledger: str,
        nickname: str,
        utterance: str,
        keeper_view: bool = False,
        decision: object | None = None,
    ) -> str:
        """按受众组装局面块（exec/14 P5.2d）。

        `audience=None` = 守秘人视图（裁决阶段用）：历史与账本全给。
        分组叙事时传该组的受众，历史/账本/原话三处一起裁。

        🔴 受众裁的是「谁看得见」，`keeper_view` 裁的是「哪一拍看得见」——
        两件事正交，所以是两个参数而不是一个。`audience=None` 恰好只在裁决
        那一拍出现，拿它兼职判断会在下一个"守秘人视角的叙事"上悄悄失效。
        """
        history = visible_history(self.history_lines, audience)
        chapters = render_chapters(visible_chapters(self.chapters, audience))
        script_recall = self._script_recall(decision)
        blocks = self.capability_blocks if keeper_view else self.narrator_capability_blocks
        party_sheet = self.party_sheet if keeper_view else ""
        # 观测：这一轮各段有多大。**只记数字不记内容**（段落里有剧本正文），
        # 判据与理由见 `keeper/context_budget.py`。
        log_turn_input(
            room_id=self.room_id,
            keeper_view=keeper_view,
            segments={
                "世界状态笔记": "\n".join(f"{k}{v}" for k, v in (self.visible_state or {}).items()),
                "在场名单": "\n".join(self.roster),
                "阶段": self.phase_status,
                "事实账本L1": ledger,
                "分段摘要L2": chapters,
                "历史窗口L3": "\n".join(history),
                "角色卡": party_sheet,
                # 分层注入时这是局面块里最大的一段（`exec/47` P1b）。
                # 🔴 它必须在这张表里：预算观测漏掉最大的那一段，等于没观测。
                "本轮相关剧本": script_recall,
                "本轮原话": utterance,
            },
            blocks=blocks,
        )
        return format_turn_input(
            self.visible_state,
            history,
            self.roster,
            nickname,
            utterance,
            phase_status=self.phase_status,
            ledger_status=ledger,
            chapters_status=chapters,
            capability_blocks=blocks,
            is_heartbeat=self.is_heartbeat,
            is_opening_ceremony=self.is_opening_ceremony,
            phase=self.phase,
            party_sheet=party_sheet,
            script_recall=script_recall,
        )

    def _script_recall(self, decision: object | None) -> str:
        """这一拍要给哪几处的剧本正文（`exec/47` P1b）。分层局才有，短模组恒为空。

        🔴 **`decision` 那一半不是可选的锦上添花。** 局面块整轮只建一次，用的是
        **裁决之前**的 `keeper_state`；而叙事那一拍最需要的恰恰是玩家刚走到的
        那个新节点的正文——它此刻只存在于裁决输出里。不带 decision 的话，
        「我去地窖」这一拍的叙事会拿不到地窖写着什么，只能瞎编。
        """
        if self.module is None:
            return ""
        mentioned = ids_mentioned_by(decision) if decision is not None else frozenset()
        focus = focus_set(
            self.module,
            self.raw_state,
            decision_node_ids=mentioned,
            decision_npc_ids=mentioned,
        )
        return render_recall(self.module, focus.node_ids, focus.npc_ids)

    def for_keeper(self, *, nickname: str, utterance: str) -> str:
        """守秘人自己的那份（裁决阶段用）。"""
        return self.render(
            audience=None,
            ledger=self.ledger_status,
            nickname=nickname,
            utterance=utterance,
            keeper_view=True,
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
    ruleset: RulesetRead | None = None,
) -> SituationBuilder:
    """读一次库，把这一轮的局面块组装器建出来。"""
    # 事实账本 L1：读全量（不设 limit）——它必须活过 HISTORY_LIMIT 的 200 条
    # 滑动窗口，这正是它存在的理由。分段摘要 L2 同理。
    async with session_factory() as db:
        known_facts = await revealed_fact_ids(db, room_id=room_id)
        chapters = await load_chapters(db, room_id=room_id)
        # 🔴 exec/34 定的「多查一次库」就落在这里：分组要知道谁在等确认会合，
        # 而那件事的唯一真相在待决定队列里。宁可多一次查询，也不在 keeper_state
        # 留一份镜像——镜像必须随权威源重建，一处漏改就长期不一致。
        merge_pending = frozenset(
            await pending_decision_manager.player_ids_of_kind(db, room_id, MERGE_CONFIRM_KIND)
        )
        # 「这些人会什么」（2026-08-14）。跟 merge_pending 同一个理由查在这里：
        # 渲染钩子拿不到 db。按 `players` 的顺序取，名册怎么排它就怎么排。
        party = await load_party_characters(db, room_id=room_id, players=players)
    return SituationBuilder(
        room_id=room_id,
        # 代码记账的键一律不原样喂给模型，判据与"state_updates 不许写"同源。
        visible_state=visible_keeper_state(keeper_state),
        history_lines=history_lines,
        roster=roster,
        phase=phase,
        phase_status=format_phase_status(phase, ending_id),
        ledger_status=render_ledger(module, known_facts),
        chapters=chapters,
        party_sheet=format_party_sheet(party, ruleset),
        # 已经垂直切出去的能力要摆在模型眼前的状态（exec/27 阶段 2）：能力不只
        # 要能改世界，还得让模型**看见**自己改成了什么样，否则下一轮只能从上
        # 一段散文里猜。
        capability_blocks=situation_blocks(
            module,
            keeper_state,
            observer_id=observer_id,
            players=tuple(players),
            merge_pending=merge_pending,
            ruleset=ruleset,
        ),
        narrator_capability_blocks=situation_blocks(
            module,
            keeper_state,
            observer_id=observer_id,
            players=tuple(players),
            merge_pending=merge_pending,
            ruleset=ruleset,
            keeper_view=False,
        ),
        is_heartbeat=is_heartbeat,
        is_opening_ceremony=is_opening_ceremony,
        # 分层注入：只有超过阈值的模组才带这两样，短模组保持 None ⇒ 召回段
        # 恒为空串 ⇒ 局面块逐字节不变（`focus.LAYERED_SCRIPT_THRESHOLD`）。
        module=module if should_layer(module) else None,
        raw_state=keeper_state if should_layer(module) else None,
    )
