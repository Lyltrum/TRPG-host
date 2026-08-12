"""🔴 `exec/28` 的地基：**流式分段拼接 == 全量 scrub，逐字节。**

这条测试是整个真流式方案的前提，也是它唯一的长期守卫。

真流式一度被判定为「会把代码强制降级成概率性的」，因为 `prose_discipline` 是
拿到完整文本后正则删的。重新读之后发现九条规则里七条的作用域有界，于是流式
可行——但**「作用域有界」这个前提靠人工声明必然退化**：下一个人加一条锚在 `$`
的新正则，什么都不会变红，而线上表现是玩家看见了本该被删掉的菜单。

所以正确性不押在注释上，押在这里：把同一段文本分别走全量和流式，断言结果
一模一样。跨作用域的新规则会当场让它红。

分片粒度故意做成多种（逐字符 / 定长 / 随机 / 一次到位）——真实 chunk 边界由
provider 决定，不能假设它落在句子上。
"""

from __future__ import annotations

import random

import pytest

from app.core.keeper.narration.prose_discipline import scrub_kp_anti_patterns
from app.core.keeper.narration.prose_stream import ProseStreamer

# ── 语料 ───────────────────────────────────────────────
# 全部是合成文本（真实模组磁带含剧本正文，只能留在 gitignored 的 tapes/），
# 但每一类都对着 prose_discipline 里一条真规则，覆盖形状而不是覆盖字面。

PLAIN = [
    "门在你身后合上，走廊尽头的灯忽明忽暗。",
    "老人抬起头，浑浊的眼睛在你脸上停了很久。",
    "雨水顺着屋檐往下淌，铁皮桶接满了又溢出来。",
    "空气里有股铁锈味，越往里走越重。",
]

MENU_TAIL = [
    "\n你可以：调查书房、询问管家、离开。",
    "\n你也可以：先回旅馆休息。",
    "\n选项：1. 上楼 2. 下地窖",
    "\n1. 敲门\n2. 绕到后院",
]

BRACKET_MENU = [
    "【你可以选择继续前进或者原路返回】",
    "[你可以查看抽屉，或者直接离开]",
]

PARENTHESIZED = [
    "（请进行侦察检定）",
    "（需要图书馆使用检定，0/1d4）",
    "(该掷一次聆听了)",
]

FAKE_LOG = [
    "[理智] 调查员：损失 2，当前 San 52。",
    "【生命】老人：损失 3，当前 HP 9。",
]

VIRTUAL = [
    "如果你走进那扇门，也许会看到别的东西。",
    "也许你可以试着敲敲窗户。",
    "你自己选吧。",
    "你可以去找邻居打听打听。",
]

MECHANIC_SENTENCE = [
    "那就该掷侦察了。",
    "现在距离近了，你该掷斗殴检定了。",
    "他的手朝你伸过来——该掷躲闪了。",
    # `exec/33 #83`：播报**不在句尾**，后面还跟着两个分句（真机原话形态）
    "你压低重心，准备摸到窗台下。该掷潜行检定了——点一下卡片，看看脚下有没有惊动什么。",
    # `exec/33 #82`：砍掉尾段后 head 只剩一个呼语，整句该丢
    "科比特消失在门廊里。阿福，该你掷侦察了。",
    # 对照：head 是真描写（同样 4 字），不许被当成呼语丢掉
    "他愣住了，该掷侦察了。",
]

#: 在场者昵称。等价性两边必须拿**同一份**——流式漏传的话，同一段文本
#: 两条路会给出不同结果，而那正是这条测试要抓的（`exec/33 #82`）。
VOCATIVES = frozenset({"阿福", "阿贵"})

TRICKY = [
    # 括号跨句：闭合前不许切
    "他压低声音（你听见他说：先别动。再等一会儿）然后退回阴影里。",
    # 句末标点后紧跟空格
    "他点了点头。 屋里安静下来。",
    # 多段
    "楼下传来脚步声。\n你屏住呼吸。\n脚步停在门口。",
    # 省略号句末
    "他张了张嘴，却什么也没说……",
    # 感叹号问号
    "谁在那儿！你听见自己的心跳。真的有人吗？",
    # 不闭合的括号（模型偶尔会写漏）——应退化成不流式，但结果仍须一致
    "他掏出一张纸条（上面写着什么已经看不清了",
    # 空白开头结尾
    "  他走进屋子。门在身后关上。  ",
]

ALL_FRAGMENTS = (
    PLAIN
    + MENU_TAIL
    + BRACKET_MENU
    + PARENTHESIZED
    + FAKE_LOG
    + VIRTUAL
    + MECHANIC_SENTENCE
    + TRICKY
)

MODES = [
    pytest.param(False, False, id="plain"),
    pytest.param(True, False, id="action_intent"),
    pytest.param(False, True, id="confused"),
    pytest.param(True, True, id="both"),
]


def _stream(text: str, *, action_intent: bool, confused: bool, chunks: list[str]) -> str:
    s = ProseStreamer(action_intent=action_intent, confused=confused, vocatives=VOCATIVES)
    out = []
    for c in chunks:
        out.append(s.feed(c))
    out.append(s.finish())
    return "".join(out)


def _split_by_size(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _split_random(text: str, rng: random.Random) -> list[str]:
    parts, i = [], 0
    while i < len(text):
        step = rng.randint(1, 7)
        parts.append(text[i : i + step])
        i += step
    return parts or [""]


def _all_chunkings(text: str, rng: random.Random) -> list[tuple[str, list[str]]]:
    return [
        ("whole", [text]),
        ("char", _split_by_size(text, 1)),
        ("size3", _split_by_size(text, 3)),
        ("size17", _split_by_size(text, 17)),
        ("random", _split_random(text, rng)),
    ]


def _assert_equivalent(
    text: str, *, action_intent: bool, confused: bool, rng: random.Random
) -> None:
    expected = scrub_kp_anti_patterns(
        text, action_intent=action_intent, confused=confused, vocatives=VOCATIVES
    )
    for name, chunks in _all_chunkings(text, rng):
        got = _stream(text, action_intent=action_intent, confused=confused, chunks=chunks)
        assert got == expected, (
            f"分片方式 {name} 下不等价\n"
            f"  原文  : {text!r}\n"
            f"  全量  : {expected!r}\n"
            f"  流式  : {got!r}"
        )


@pytest.mark.parametrize("action_intent,confused", MODES)
@pytest.mark.parametrize("fragment", ALL_FRAGMENTS)
def test_single_fragment_equivalent(fragment: str, action_intent: bool, confused: bool) -> None:
    _assert_equivalent(
        fragment, action_intent=action_intent, confused=confused, rng=random.Random(0)
    )


@pytest.mark.parametrize("action_intent,confused", MODES)
def test_random_compositions_equivalent(action_intent: bool, confused: bool) -> None:
    """随机拼接片段——覆盖我想不到的组合。seed 固定，红了必可复现。"""
    rng = random.Random(20260803)
    for _ in range(400):
        n = rng.randint(1, 5)
        text = "".join(rng.choice(ALL_FRAGMENTS) for _ in range(n))
        _assert_equivalent(text, action_intent=action_intent, confused=confused, rng=rng)


@pytest.mark.parametrize("action_intent,confused", MODES)
@pytest.mark.parametrize("text", ["", "   ", "\n", "。", "你可以：A"])
def test_degenerate_inputs_equivalent(text: str, action_intent: bool, confused: bool) -> None:
    _assert_equivalent(text, action_intent=action_intent, confused=confused, rng=random.Random(1))


def test_streamer_keeps_raw_text() -> None:
    """落库/记账用的是完整原文，不是拼起来的 delta——两者本来就不相等。"""
    s = ProseStreamer(action_intent=False)
    for c in ["他走进", "屋子。\n你可以：离开"]:
        s.feed(c)
    s.finish()
    assert s.raw == "他走进屋子。\n你可以：离开"


def test_menu_tail_never_reaches_the_player() -> None:
    """这条测试是这一整套的目的本身，单独钉一遍：菜单尾巴一个字都不许推出去。"""
    text = "他推开门，屋里空无一人。\n你可以：搜查抽屉、离开房间。"
    s = ProseStreamer(action_intent=False)
    emitted = [s.feed(ch) for ch in text]
    assert "你可以" not in "".join(emitted)
    assert "你可以" not in s.finish()
