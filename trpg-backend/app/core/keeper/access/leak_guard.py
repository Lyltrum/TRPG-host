"""泄密守门（exec/14 P3）—— 语义轴上第一个能变红的守门人。

## 在此之前：运行时零守门人

`prose_discipline.py` 那一整套 scrub 规则管的是**守秘人说话的方式**（菜单、
虚拟挡、机制播报、伪造记账），没有一条管**内容泄密**；`validate_module.py`
的 `check_leak` 只在**组装期静态**扫 `player_intro` / `opening.script` 两个字段。
也就是说：对局中吐给玩家的每一句叙事，此前没有任何东西检查过它有没有说漏。

## 这一层守的是什么：元层，硬不变量

`tier=meta` 的事实（`kp_truth.key_facts` 那一层）对**任何虚构内主体永不可见**，
不存在"挣得"这回事，因此"它出现在叙事正文里"没有任何合法解释——这是一条
**没有例外的硬不变量**，适合做自动拦截。

对比之下，`tier=diegetic` 的线索**本来就是要在对局中揭示的**：NPC 主动说出、
守秘人判断时机成熟给出，都是合法主持。对它做"未挣得就拦"会疯狂误伤合法play，
所以这里**不碰** diegetic——它属于 P4 有了线索账本、P5 有了 per-observer 投递
之后的事。

## 判据：连续 14 字逐字重合

只认**逐字复述**，不做语义判断——判定必须零误报才配当自动拦截。

阈值怎么来的：元层事实实测中位 28 字（5 模组 25 条），14 字是半条断言；
拿 5 个模组的元层事实与它们自己的玩家可见文本（`player_intro` / 各处
`public_text`）对撞，**14 字连续重合命中 0 条**——阈值站得住。

放弃过一版"按标点切片段、片段命中只记日志"的两档设计：实测 32% 的元层事实
根本没有内部标点（切不出片段），而能切出来的片段里大量是公开人名地名，
信号会被噪音淹没。宁可只保留一个零误报的硬判定。

## 🔴 诚实的边界：这不等于"结构上不可能"

真正的结构性保证要求**叙事从 `view(S)` 生成**，模型物理上拿不到不该说的。
但守秘人**必须知道秘密才能主持**（要绕开它、要埋伏笔），所以它的上下文里
永远有全本剧本——对 KP 自己的正文，只能是"事后检查"而不是"拿不到"。

结构性保证的落点在 **P5/P6**：那时发给某个玩家/由某个 NPC 说出的内容，是从
那个主体的视图生成的，KP 仍然全知，但**输出通道**是按视图算出来的。
本层是那之前的兜底，也是唯一一个现在就能变红的泄密断言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from app.core.keeper.contract.module_loader import ScenarioModule

logger = structlog.get_logger()

#: 判定阈值：元层事实里连续这么多字原样出现在叙事里，就算逐字复述。
#: 见 docstring 的校准记录——5 个模组零误伤。
_MIN_VERBATIM_RUN = 14

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？])")

#: 整段都被判为泄密时的替代文案。
#: 🔴 这里的取舍与 `prose_discipline` 相反：那边砍空后**退回原文**（被砍的是
#: 菜单/机制播报这类格式问题，退回去顶多难看）；这边砍空后**绝不退回原文**
#: （被砍的是模组真相，退回去就毁了整局）。宁可让玩家看到一句无信息量的
#: 过场，也不能把元层放出去。
_EMPTIED_PLACEHOLDER = "守秘人顿了顿，像是把要说的话咽了回去。"


@dataclass(frozen=True)
class LeakHit:
    fact_id: str
    #: 命中的那段原文，用于定位到具体句子并丢弃
    matched: str


def _meta_facts(module: ScenarioModule) -> list[tuple[str, str]]:
    return [(f.id, f.text.strip()) for f in module.facts if f.tier == "meta" and f.text.strip()]


def _longest_run_in(fact_text: str, text: str) -> str:
    """`fact_text` 里最长的、原样出现在 `text` 中的连续片段（不足阈值则空）。"""
    longest = ""
    for start in range(len(fact_text) - _MIN_VERBATIM_RUN + 1):
        for end in range(len(fact_text), start + _MIN_VERBATIM_RUN - 1, -1):
            run = fact_text[start:end]
            if len(run) > len(longest) and run in text:
                longest = run
                break
    return longest


def scan_meta_leaks(text: str, module: ScenarioModule) -> list[LeakHit]:
    """扫描正文里逐字复述的元层断言。**每条元层事实在每一句里各报一次**。

    🔴 粒度是**逐句**，不是全文一次（`exec/28` 4.5）。此前是全文扫一次、每条
    fact 只留最长的那个命中，而丢弃是按句做的（见 `scrub_meta_leaks`）——于是
    同一条真相泄漏在多句里时，较短的那句会被 argmax 挤掉、**原样发给玩家**。
    元层不可见是硬不变量（`exec/14 P3`），那是真漏。

    判据本身没有放宽：仍然是「连续 ≥ `_MIN_VERBATIM_RUN` 字逐字重合」，所以
    误伤面不变。**改小那个阈值会同时在两个方向放大误伤，动它之前先看 exec/28
    4.5 的风险表。**

    同一个 `fact_id` 因此可能出现多条命中（一句一条）。要统计"泄了几条事实"
    的话记得按 `fact_id` 去重——这里给的是"泄了几次"。
    """
    if not text:
        return []
    facts = _meta_facts(module)
    if not facts:
        return []
    # 先全文快扫一次：没有任何命中就不必逐句再扫（常见情况，避免 ×句数的开销）
    if not any(_longest_run_in(fact_text, text) for _, fact_text in facts):
        return []

    hits: list[LeakHit] = []
    for sentence in _split_sentences(text):
        for fact_id, fact_text in facts:
            longest = _longest_run_in(fact_text, sentence)
            if longest:
                hits.append(LeakHit(fact_id=fact_id, matched=longest))
    return hits


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s]


def drop_leaking_sentences(text: str, module: ScenarioModule) -> tuple[str, list[LeakHit]]:
    """丢掉逐字复述元层断言的那些句子。**删光就返回空串，不做占位兜底。**

    流式路径按段调用它（`exec/28` 第 3 步）：占位文案是**整段**级的决定
    （"这一轮守秘人什么都没说成"），一个片段被删光不等于整轮被删光，在这里
    塞占位会让玩家在一段话中间读到"守秘人顿了顿"。
    """
    hits = scan_meta_leaks(text, module)
    if not hits:
        return text, hits

    bad_runs = [h.matched for h in hits]
    kept = [
        sentence
        for sentence in _split_sentences(text)
        if not any(bad in sentence for bad in bad_runs)
    ]
    return "".join(kept).strip(), hits


def scrub_meta_leaks(text: str, module: ScenarioModule) -> tuple[str, list[LeakHit]]:
    """整段版：在 `drop_leaking_sentences` 之上补一层「删光了怎么办」。

    整句丢弃与 `prose_discipline` 里 `_SENTENCE_DROP` / `_FAKE_STAT_LOG_LEAK`
    的处理方式一致——不整段丢，否则玩家看到的是空气。
    """
    cleaned, hits = drop_leaking_sentences(text, module)
    if not hits:
        return cleaned, hits
    return (cleaned or _EMPTIED_PLACEHOLDER), hits


def log_leak_hits(hits: list[LeakHit], *, room_id: str | None) -> None:
    """把命中记进结构化日志，供以后统计真实泄密率。"""
    if not hits:
        return
    logger.warning(
        "keeper_meta_leak",
        room_id=room_id,
        facts=[h.fact_id for h in hits],
        runs=[h.matched for h in hits],
    )
