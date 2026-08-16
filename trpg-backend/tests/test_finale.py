"""命中剧本预设结局要写成终章（2026-08-16 真机）。

实测：34 拍打完，`ending_reached=ending-adventure-options`、`对局阶段=finished`
——**收尾在后端成立了，而玩家侧完全看不出来**。那一拍的叙事写的是"警察按住
科比特、有人去封绳、有人打电话叫支援"，读起来像下一拍还会继续；真正的终止发生
在下一次发言，回的是一句系统提示。

根因：`inject_closure_guidance` 只在 `reopened_from_ending` 时注入，而
`ending_reached` 这条路 `progression` 当轮直接置 finished、**不经过 `ending`
阶段**，于是一条纪律都注入不到。同一件事两条路，此前只接通了开放式那条。

顺带：模组自己写的落幕（`endings[].text`）此前只躺在系统 prompt 末尾的剧本
全文里，跟没命中的那几条结局并排——**从没被点名喂给叙事**。
"""

from __future__ import annotations

from app.core.keeper.narration.prose_discipline import (
    inject_closure_guidance,
    inject_finale_guidance,
    narration_limit,
)


def test_the_finale_tells_it_to_stop_not_to_linger() -> None:
    g = inject_finale_guidance("")
    assert "最后一段" in g
    # 🔴 跟收束纪律的分水岭：终章不许再留动作机会
    assert "不要再留动作机会" in g
    assert "不抛新线索" in g


def test_the_finale_is_not_the_closure_guidance() -> None:
    """🔴 两段不许复用：收束（`ending`）是**可撤回**的中间态，要"留最后一次
    动作机会"；终章（`finished`）已经落幕，再留动作机会就是自相矛盾——那一拍
    之后任何行动都会被硬拒。"""
    closure = inject_closure_guidance("")
    finale = inject_finale_guidance("")
    assert "留一次最后的动作机会" in closure
    assert "留一次最后的动作机会" not in finale
    assert closure != finale


def test_the_module_written_ending_is_handed_to_the_narrator() -> None:
    """剧本写好的落幕要点名喂进去，不能指望它自己去剧本全文末尾翻。"""
    g = inject_finale_guidance("", "警车灯照亮整条街，屋里的东西再没出现过。")
    assert "警车灯照亮整条街" in g
    assert "按它收" in g


def test_no_module_ending_text_still_gets_the_discipline() -> None:
    """开放式模组没有结局正文——纪律照给，不因为缺数据就整段不注入。"""
    g = inject_finale_guidance("", None)
    assert "最后一段" in g
    assert "按它收" not in g


def test_existing_guidance_is_kept() -> None:
    g = inject_finale_guidance("镜头给那扇门。", "尘埃落定。")
    assert "镜头给那扇门。" in g and "尘埃落定" in g


def test_injecting_twice_does_not_stack() -> None:
    once = inject_finale_guidance("原文")
    assert inject_finale_guidance(once) == once


def test_the_finale_gets_the_longer_budget() -> None:
    """终章要讲清结果，字数上限跟收尾阶段同档（这一条改动前就成立，钉住别退化）。"""
    assert narration_limit(ending_reached=True) > narration_limit()
