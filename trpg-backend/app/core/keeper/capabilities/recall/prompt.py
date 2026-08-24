"""recall 的 prompt 块（`exec/47` P2）。

起点是 2026-08-24 那局实测：玩家问「借书卡夹在第几页」，模型确信地答「第 87 页」
（真值 88）并补了一张不存在的插页。**而原文在库里出现过 6 次**——滚出 L3 窗口的
是"注入"不是"存储"。

⇒ 这一片要模型做的事只有一件：**发现玩家在问过去，就说出要查什么**。
查什么、查得到查不到、查回来怎么用，都不归它管（分别是 `memory/recall.py` 与
召回段自己那段文字）。
"""

from __future__ import annotations

from app.core.keeper.contract.registry import PromptBlock

_RULE = PromptBlock(
    slot="rules",
    # 紧跟 4h（既成事实 4.98）之后：两者是同一件事的两半——一个管"记下来"，
    # 一个管"回头找得回来"。
    order=4.99,
    text=(
        "4i. **玩家在打听过去发生的事**：把要查的东西写进 `recall_query`，"
        '例如 `"借书卡 林业志 页数"`。\n'
        "   什么时候写：他问的是**这局里已经发生过、但他（或你）可能记不清**的"
        "具体东西——他记下的号码、他藏东西的位置、他给什么起的外号、某个 NPC "
        "说过的原话、某样东西上写着的字。这类发言你通常也会把 `player_state` "
        "判成 `question_to_kp`。\n"
        "   🔴 **写关键词，不要写整句问句**：「林间怪火 记载」查得到，"
        "「我抄进笔记本的那场林间怪火是哪一年」会被"
        "「抄进笔记本」带偏（实测如此）。**不要把你猜的答案写进去**——"
        "那样查回来的只会是你自己猜的东西。\n"
        "   🔴 **不写这些**：他在问接下来该做什么（那是 confused）· "
        "他在问能不能做某件事（那是 feasibility_question）· "
        "他问的是剧本设定而不是这局发生过的事 · 他只是在行动。"
    ),
)

_EXAMPLE = PromptBlock(
    slot="output_example",
    order=77,
    text='  "recall_query": null',
)

PROMPT_BLOCKS = (_RULE, _EXAMPLE)
