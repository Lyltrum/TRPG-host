"""recall 能力贡献给 `KeeperDecision` 的字段片段（`exec/47` P2）。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class RecallDecisionFields(DecisionModel):
    """玩家在打听一件过去的事时，这一拍要去历史里查什么。

    🔴 **只有它是自由文本，而这是刻意的**：它不是标识符（不参与任何代码判断、
    不进任何表），它是**检索词**——拿去 BM25 匹配历史原文，命中与否由分数决定。
    「不要用自由文本当标识符」那条判据管的是标识符，不是查询词。

    🔴 **没有默认值以外的形态**：绝大多数拍都不该填。填了就要多查一次库，
    而回忆型提问在实测里占比很低（265 拍的局里个位数）。
    """

    recall_query: str | None = Field(
        default=None,
        description=(
            "玩家这一拍在向你打听一件**过去发生过的事**时，把要查的东西写成"
            "几个关键词（「借书卡 林业志 页数」「车牌 笔记本」）。"
            "其余情况留空。"
        ),
    )


#: 🔴 **故意是空的**：它不改任何状态，只决定这一拍多读一段历史。
FIELD_CAPABILITIES: Mapping[str, Capability] = {}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    return {"recall_query": getattr(decision, "recall_query", None)}
