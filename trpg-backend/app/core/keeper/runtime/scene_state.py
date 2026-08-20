"""场景指针编解码（04 遗留项，2026-07-30 结构化）。

跟 `phase.py`/`visibility.py`/`agenda_state.py` 同一套模式：KEY 常量 +
`load_*`。这里没有独立的 `format_*`——`current_node_id` 不作为单独的
"局面块"小节展示给 LLM（它只是 `check_guard` 检定护栏、场景切换过渡引导
判断的内部依据），不像 phase/agenda/visibility 那样需要渲染成一段人类
可读文本单独展示。写入侧在 `location_state.set_current_node_impl`。

取代此前对「当前场景」自由文本人类地名做的模糊字符串匹配——裁决 LLM
从剧本节点树里选真实 id（`module.node_by_id` 校验，非法 id 拒绝写入）。
"""

from __future__ import annotations

CURRENT_NODE_KEY = "当前场景节点"

#: 同一件事的人类可读那一面：裁决器用 `state_updates` 写的地名。
#:
#: 🔴 两个键必须一起看：`world_state` 写地名、`movement` 维护节点指针，两者脱节
#: 就是 `exec/19 #48`（人已经站在屋外，护栏还拿地下室的 checks[] 卡他）。常量
#: 放在这里而不是任一片能力里，是因为**两片都要用它**——放进其中一片就会造成
#: 跨能力 import。
SCENE_NAME_KEY = "当前场景"


def load_current_node_id(keeper_state: dict | None) -> str | None:
    if not keeper_state:
        return None
    raw = keeper_state.get(CURRENT_NODE_KEY)
    if raw is None or raw == "":
        return None
    return str(raw).strip() or None
