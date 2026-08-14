"""悬而未决的事：存储形态 + 局面块。

## 它补的是哪个洞

真人实测（`exec/31`）实据一句：`「拉开距离，但米-戈仍在追击」`。

「米-戈仍在追击」是一个**正在进行的处境**，决定接下来每一轮该怎么演——可它
不是节点、不是议程、不是任何保留键，**只活在那一段散文里，下一轮模型就忘了**。

🔴 **注意它跟 `state_updates` 的区别，不然会以为这一片是重复的。**
`world_state` 那条路要求 `subject` 取自剧本白名单（NPC id / 节点 id / world），
而且它是**键值覆盖**：没有"这件事还在不在"的概念。即兴出来的处境两头都不合——
它没有剧本 id 可挂，也需要一个**显式的结束**。没有结束就是 `#46` 那个形状：
写进去之后永远挂着，模型每轮都被提醒"米-戈还在追"，追到天荒地老。

## 形态照抄即兴地点（`exec/32`）

**这是那套形态的第二个实例**，所以照抄而不是另起一套：模型只给**文本**，
**id 由代码分配**（`thread-N`）；关闭时只能从局面块列出的 id 里挑。
让模型自己起 id 就是「不要用自由文本当标识符」的复发——「米-戈追击」
「米戈仍在追」会变成两条。

存储是 dict（同 `IMPROVISED_LOCATION_KEY` / `NPC_STATE_KEY`）：
`{"thread-1": {"text": "米-戈仍在追击"}}`。
"""

from __future__ import annotations

from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY

#: 悬而未决的事。由本能力的 `reserved_state_keys` 声明出去，`state_updates` 改不动。
OPEN_THREADS_KEY = "悬而未决"

#: id 前缀。**代码分配**，模型只能从局面块里挑已有的。
THREAD_ID_PREFIX = "thread-"

#: 已经发到第几号。
#:
#: 🔴 **不能从表里现算**（第一版就是那么写的，测试当场抓住）：这张表跟即兴
#: 地点表的区别正是**这里的条目会被删掉**。关掉 thread-1 之后表里最大号退回
#: 0，下一条又叫 thread-1——两件不同的事在事件流里共用一个 id，复盘时分不开。
#: 「只增不复用」这句话要成立，就得有个地方记住"发到几号了"，表本身记不住。
OPEN_THREADS_SEQ_KEY = "悬而未决序号"

#: 超过这个条数打一条 warning。**膨胀本身是信号，不是要治的病**（判据同即兴
#: 地点的软上限）：它说明模型在拿这张表当便签本，那时该查的是"它把什么东西
#: 塞进来了"，而不是给这张表加裁剪。
OPEN_THREADS_SOFT_LIMIT = 12


def load_open_threads(keeper_state: dict | None) -> dict[str, dict]:
    """解析悬而未决表。形状不对的条目整条丢弃，不产生半条记录。

    `node` 是**这条处境在哪成立的**（2026-08-14 加，见 `format_open_threads`）。
    老对局的条目没有这个键，读出来是 `None` —— 那表示"不知道在哪成立"，
    按"到处都成立"处理，与加这个字段之前**逐字一致**。
    """
    if not keeper_state:
        return {}
    raw = keeper_state.get(OPEN_THREADS_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for thread_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if not thread_id or not text:
            continue
        node = payload.get("node")
        entry: dict = {"text": text}
        if isinstance(node, str) and node.strip():
            entry["node"] = node.strip()
        out[str(thread_id)] = entry
    return out


def load_thread_seq(keeper_state: dict | None) -> int:
    """已经发到第几号。没记过就从表里现算——老对局（这一片能力上线之前建的房间）
    没有这个键，那时表里最大号就是最好的下界，不会倒退。"""
    raw = (keeper_state or {}).get(OPEN_THREADS_SEQ_KEY)
    if isinstance(raw, int) and raw >= 0:
        return raw
    return _max_id_in(load_open_threads(keeper_state))


def _max_id_in(table: dict[str, dict]) -> int:
    used = 0
    for thread_id in table:
        if thread_id.startswith(THREAD_ID_PREFIX):
            suffix = thread_id[len(THREAD_ID_PREFIX) :]
            if suffix.isdigit():
                used = max(used, int(suffix))
    return used


def next_thread_id(seq: int) -> tuple[str, int]:
    """下一个 id 与新的计数。**只增不复用**——复用会让复盘里两件事共用一个 id。

    吃计数而不是吃表：表里的条目会被关掉删除，从表现算就会回填已经用过的号。
    """
    return f"{THREAD_ID_PREFIX}{seq + 1}", seq + 1


def format_open_threads(keeper_state: dict | None) -> str:
    """局面块正文。一条都没有时返回空串——整块不渲染（退化保证）。

    🔴 **必须全量列出，不许"只显示最近 N 条"**：这块就是模型挑 id 的白名单，
    没列出来的对它等于不存在，它会把同一件事重新开一条。裁剪只能针对存储，
    不能针对展示（判据同即兴地点，`exec/32 §7.2`）。

    ## 🔴 按「在哪成立」分两组（2026-08-14 真人实测）

    实测里一条 `「看护仍在身后追赶」`在疗养院开出来，然后**跟着玩家跑了 25
    分钟、跨了三个地点**：开车两小时到度假屋、下地窖、钻土室，裁决每一轮都在
    写"看护仍在追，威胁成立"——最后跟米-戈同框。

    根因不是模型笨，是**这条 thread 没有作用域**：它只有 `{id, text}`，
    没有"在哪儿才算数"，于是每一轮都被原样读到。

    修法不是自动关掉（追击**可以**跨地点，自动关会误杀），而是**把判断的输入
    摆准**：离开原地之后单列一组，明说"多半已经不成立"。关不关仍由模型决定，
    但它现在看得见这条信息了。这跟 `san_check` 那条判据同源——
    **能确定化的是判断的输入，不是判断本身。**
    """
    table = load_open_threads(keeper_state)
    if not table:
        return ""
    here = (keeper_state or {}).get(CURRENT_NODE_KEY)

    def _line(thread_id: str, entry: dict) -> str:
        return f"- {thread_id}：{entry['text']}"

    # 没记 node 的（老对局）算"到处都成立"，跟加这个字段之前一致。
    standing = {
        tid: e for tid, e in table.items() if not e.get("node") or not here or e["node"] == here
    }
    left_behind = {tid: e for tid, e in table.items() if tid not in standing}

    parts = []
    if standing:
        parts.append(
            "这些事还悬着，**每一轮都仍然成立**，叙事时要把它们算进当前处境；"
            "已经了结的（威胁被摆脱、期限到了、东西找到了）必须写进 "
            "`resolved_threads`——不写就一直挂着。\n"
            + "\n".join(_line(tid, e) for tid, e in standing.items())
        )
    if left_behind:
        parts.append(
            "🔴 下面这几条是在**别的地方**开的，调查员已经离开那里了——"
            "除非它明确追了过来（对方会追、期限还在走），否则它**已经不成立**，"
            "这一轮就该写进 `resolved_threads` 关掉，更不要拿它当当前的威胁来演：\n"
            + "\n".join(f"{_line(tid, e)}（发生在 {e['node']}）" for tid, e in left_behind.items())
        )
    return "\n\n".join(parts)


def render_open_threads(context: SituationContext) -> str:
    """注册进局面块的 situation 钩子。"""
    return format_open_threads(context.keeper_state)
