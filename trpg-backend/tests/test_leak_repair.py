"""泄密要能被修掉，不是只能被拒绝。

## 为什么补这条

方向定了：**导入只能成功，拒绝是兜底不是常态**。照这个标尺审一遍自修的覆盖，
每一类硬失败都有路径，只有 `leak` 例外——它是跨实体错误，唯一的修法是**整份
重吐**，而那条路在真实体量的模组上是结构性失败（林中屋产物 25407 字符，自修
响应截在 21916，三次尝试全断在同一位置）。所以 leak 实际上修不掉，只能变成
一次拒绝。它也确实是修完前面几类之后**唯一还在拒绝的类别**。

但泄密的作用域其实很小：`check_leak` 只扫 `player_intro` 和 `opening.script`
两个字段，各是一段话。重吐一段话输出长度有界——跟分片修实体是同一个道理。

## 不做的事

改写完仍然泄密就是真失败，照旧拒绝。**不做「把这段删掉」之类的兜底**：
删掉玩家开场白是把模组悄悄弄残，比拒绝更糟。
"""

from __future__ import annotations

from scripts.module_probe.assemble import (
    leaky_fields,
    read_leaky_field,
    write_leaky_field,
)


def test_parses_which_field_and_which_words() -> None:
    """自修要知道**改哪个字段、避哪些词**，两样都在那句错误里。"""
    errors = [
        "真相关键词 '精神崩溃' 出现在玩家可见字段 player_intro",
        "真相关键词 '献祭仪式' 出现在玩家可见字段 player_intro",
        "真相关键词 '献祭仪式' 出现在玩家可见字段 opening.script",
    ]

    assert leaky_fields(errors) == {
        "player_intro": ["精神崩溃", "献祭仪式"],
        "opening.script": ["献祭仪式"],
    }


def test_unknown_fields_are_ignored_not_guessed() -> None:
    """只认 `check_leak` 真会扫的那两个字段。

    多认一个就会去改一个没被检查的地方——改了也不会让校验变绿，白花一次调用，
    而且看起来像"修了但没用"。
    """
    assert leaky_fields(["真相关键词 'x' 出现在玩家可见字段 node.kp_text"]) == {}
    assert leaky_fields(["[schema] 别的错误"]) == {}


def test_reads_and_writes_both_fields() -> None:
    """两个字段的路径不一样（一个顶层、一个嵌在 opening 里），两条都要通。"""
    module = {"player_intro": "旧的开场", "opening": {"script": "旧的脚本"}}

    assert read_leaky_field(module, "player_intro") == "旧的开场"
    assert read_leaky_field(module, "opening.script") == "旧的脚本"

    write_leaky_field(module, "player_intro", "新的开场")
    write_leaky_field(module, "opening.script", "新的脚本")

    assert module == {"player_intro": "新的开场", "opening": {"script": "新的脚本"}}


def test_missing_opening_does_not_crash() -> None:
    """`opening` 可以整个不存在——读到空字符串，写入静默跳过（没地方可写）。"""
    module: dict[str, object] = {"player_intro": "开场"}

    assert read_leaky_field(module, "opening.script") == ""
    write_leaky_field(module, "opening.script", "x")

    assert "opening" not in module


def test_leak_no_longer_falls_through_to_the_whole_module_rewrite() -> None:
    """🔴 接线本身要有测试守着。

    `leak` 必须被从"整份重吐"那一批里排除掉——否则这条新路径加了也白加，
    它照样会去走那条吐不完的路。
    """
    import inspect

    from scripts.module_probe import assemble

    body = inspect.getsource(assemble.run_pipeline)

    assert "repair_leaky_text" in body, "自修循环没接上泄密的分片修法"
    assert 'not e.startswith("[leak]")' in body, "leak 仍然落进整份重吐那一批"


# ── 改写调用本身 ──────────────────────────────────────


class _StubCompletions:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: dict[str, object] = {}

    def create(self, **kwargs):
        self.seen = kwargs

        class _M:
            content = self.reply

        class _C:
            message = _M()
            finish_reason = "stop"

        class _U:
            prompt_tokens = 7
            completion_tokens = 3
            total_tokens = 10

        class _R:
            choices = [_C()]
            usage = _U()

        return _R()


def _stub(reply: str):
    inner = _StubCompletions(reply)
    return type("_Cli", (), {"chat": type("_Ch", (), {"completions": inner})()})(), inner


def test_the_forbidden_words_and_the_text_both_reach_the_model() -> None:
    """改写要同时拿到「避开哪些词」和「原文」。

    少给任何一样它都只能猜——而猜正是这条路径要避免的。
    """
    from scripts.module_probe.assemble import CallStats, repair_leaky_text

    client, inner = _stub("改写后的开场白")
    stats = CallStats()

    out = repair_leaky_text(
        client,
        field="player_intro",
        text="你听说那位教授精神崩溃了。",
        keywords=["精神崩溃"],
        stats=stats,
        label="repair#1.leak:player_intro",
    )

    assert out == "改写后的开场白"
    sent = " ".join(m["content"] for m in inner.seen["messages"])
    assert "精神崩溃" in sent, "没告诉模型要避开哪个词"
    assert "你听说那位教授" in sent, "没把原文给它"
    assert inner.seen["tape_key"] == "repair#1.leak:player_intro", "没带磁带子键，回放会错位"


def test_it_is_counted_like_every_other_call() -> None:
    """记账不能漏：漏了成本报表就少算，而它是真花了钱的一次调用。"""
    from scripts.module_probe.assemble import CallStats, repair_leaky_text

    client, _ = _stub("新文本")
    stats = CallStats()

    repair_leaky_text(
        client, field="player_intro", text="旧", keywords=["x"], stats=stats, label="l"
    )

    assert (stats.calls, stats.prompt_tokens, stats.completion_tokens) == (1, 7, 3)


def test_an_empty_rewrite_fails_loudly() -> None:
    """空回复不能当成"改好了"——那会把玩家开场白悄悄清空。"""
    import pytest

    from scripts.module_probe.assemble import CallStats, repair_leaky_text

    client, _ = _stub("   ")

    with pytest.raises(RuntimeError):
        repair_leaky_text(
            client, field="player_intro", text="旧", keywords=["x"], stats=CallStats(), label="l"
        )
