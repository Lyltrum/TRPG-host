"""把叙事的 token 流切成「已经可以安全推给玩家」的片段（`exec/28`）。

## 为什么需要它

`prose_discipline` 的纪律层是**拿到文本后用正则删**的。真流式意味着字已经在
玩家屏幕上、删不掉，所以曾被判定为「会把代码强制降级成概率性的」而推翻。

重新逐条读之后：**九条规则里七条的作用域是有界的**（一句 / 一个括号块 / 一行），
只有两条真的需要全文——

- `_MENU_TAIL`：锚在 `$`，`re.S` 的 `.*` 会从**最后一个 `\\n`** 吃到结尾；
- 全滤空退化：要知道"全都没了"。

于是流式是可行的，只要每一段都等到**它自己的作用域闭合**再推。

## 安全切点的三个条件

1. **在句末标点之后**（`。！？…`）——按句是绝大多数规则的作用域上界。
   🔴 刻意**不含 `\\n`**：片段若以换行结尾，全量路径在整段末尾做的 `rstrip` /
   收尾修剪会把它删掉，而流式已经推出去了，当场不等价。
2. **括号全部闭合**——`_BRACKET_MENU`（最长 120 字）和
   `_PARENTHESIZED_MECHANIC`（60 字）都可能跨句。模型写了个不闭合的括号时
   这里会一路扣到 `finish()`，等于退化成不流式，是安全的方向。
3. 🔴 **不越过 buffer 里最后一个 `\\n`。** 这条是 `_MENU_TAIL` 的正确性依据：
   它只从最后一个 `\\n` 开始匹配，把那个 `\\n` 及其后的一切留在 `finish()` 里，
   它的作用域就完整落在扣留部分内，推出去的内容不可能被它影响。

## 正确性靠什么保证

不靠上面这段推理——靠 `tests/test_prose_stream_equivalence.py`：**流式分段
拼接的结果必须与全量 scrub 逐字节相等**。作用域有界这个前提靠人工声明必然
退化（下一个人加一条锚 `$` 的正则，什么都不会变红），只有那条测试守得住。
"""

from __future__ import annotations

from .prose_discipline import (
    _MENU_TAIL,
    _TRAILING_JUNK,
    degrade_when_scrubbed_empty,
    scrub_bracket_blocks,
    scrub_sentence_scoped,
)

# 句末标点。🔴 不含 `\n`，理由见模块 docstring 条件 1。
_SENTENCE_END = "。！？…"
_OPEN = "（(【["
_CLOSE = "）)】]"


class ProseStreamer:
    """按安全切点把叙事流切段。

    用法：
        s = ProseStreamer(action_intent=..., confused=...)
        for chunk in llm_stream:
            delta = s.feed(chunk)
            if delta:
                推给玩家
        tail = s.finish()
        if tail:
            推给玩家
    """

    def __init__(self, *, action_intent: bool, confused: bool = False) -> None:
        self._action_intent = action_intent
        self._confused = confused
        self._raw = ""
        self._cut = 0
        self._emitted_any = False

    @property
    def raw(self) -> str:
        """收到的全部原始文本。落库/记账用完整文本，不是拼 delta。"""
        return self._raw

    def feed(self, chunk: str) -> str:
        """吃一段 token，返回这次可以安全推出去的文本（可能是空串）。"""
        if chunk:
            self._raw += chunk
        # 🔴 全量路径开头有一次 `text.strip()`，前导空白必须同样剥掉——留着的话
        # `_MENU_TAIL` 会从**开头那个换行**开始匹配（它以 `\n+` 起头），把整段
        # 吃光，然后掉进退化分支，结果与全量差得很远。等价性测试抓到的第一个 bug。
        # `_cut == 0` 时才剥：还没推出任何东西，下标不会错位。
        if self._cut == 0:
            self._raw = self._raw.lstrip()
        end = self._safe_end()
        if end <= self._cut:
            return ""
        piece = self._raw[self._cut : end]
        self._cut = end
        out = scrub_sentence_scoped(
            scrub_bracket_blocks(piece),
            action_intent=self._action_intent,
            confused=self._confused,
            # 整段的首尾只有一次：开头归第一个非空片段，结尾归 finish()
            trim_head=not self._emitted_any,
            trim_tail=False,
        )
        if out:
            self._emitted_any = True
        return out

    def finish(self) -> str:
        """流结束。处理扣留的尾巴，这里才轮到 `_MENU_TAIL` 和收尾修剪。"""
        if self._cut == 0:
            self._raw = self._raw.lstrip()
        # 整段的尾部一定落在扣留部分里，所以全量那次 `strip()` 的后半在这里补上
        rest = self._raw[self._cut :].rstrip()
        self._cut = len(self._raw)

        body = scrub_bracket_blocks(rest)
        body = _MENU_TAIL.sub("", body).rstrip()
        body = scrub_sentence_scoped(
            body,
            action_intent=self._action_intent,
            confused=self._confused,
            trim_head=not self._emitted_any,
            trim_tail=True,
        )
        body = _TRAILING_JUNK.sub("", body)

        if self._emitted_any or body:
            return body
        # 一个字都没推出去 → 整段被滤空，走跟全量同一条退化路径
        original = self._raw.strip()
        if not original:
            return ""
        if self._confused and not self._action_intent:
            return original
        return degrade_when_scrubbed_empty(self._raw)

    def _safe_end(self) -> int:
        """在 `_raw[_cut:]` 里找最靠后的安全切点，返回它在 `_raw` 里的下标。"""
        best = self._last_sentence_end(len(self._raw))
        if best <= self._cut:
            return self._cut
        # 🔴 再让 `_MENU_TAIL` 自己审一遍这个候选前缀。
        #
        # 一开始我以为它「只从最后一个 `\n` 开始匹配」，扣留最后一段就够——
        # **错的**，等价性测试当场抓到：`re.sub` 取最左匹配，它可以从**任何**
        # 一个 `\n` 起头，只要后面接菜单模式、`.*`（re.S）能吃到结尾。
        #
        # 这里不另写一份"菜单开头"的正则去猜（那就是同一份知识写两处，迟早不
        # 一致），直接拿 `_MENU_TAIL` 试探候选前缀 P：
        # 若它在完整文本 T = P + S 上的匹配起点 i < len(P)，那么同一个起点在 P
        # 上也必然匹配到 P 末尾（`.*` 可长可短，`\s*$` 照样满足）。所以
        # 「P 上没命中」⟹「T 上的匹配起点不早于 P 的末尾」⟹ P 推出去是安全的。
        candidate = self._raw[self._cut : best]
        hit = _MENU_TAIL.search(candidate)
        if hit is None:
            return best
        return self._last_sentence_end(self._cut + hit.start())

    def _last_sentence_end(self, limit: int) -> int:
        """`_raw[_cut:limit)` 里最靠后的「句末标点 + 括号已闭合」位置。"""
        raw = self._raw
        depth = 0
        best = self._cut
        for i in range(self._cut, limit):
            ch = raw[i]
            if ch in _OPEN:
                depth += 1
            elif ch in _CLOSE:
                depth = max(0, depth - 1)
            elif depth == 0 and ch in _SENTENCE_END:
                best = i + 1
        return best
