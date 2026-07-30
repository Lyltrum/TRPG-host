"""事实迁移脚本（exec/14 P1.3）。

用合成 raw dict 构造，不碰 `模组资料/`（gitignore，CI 里没有）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.migrate_facts import migrate  # noqa: E402


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "meta": {"id": "m", "title": "合成"},
        "kp_truth": {"summary": "真相", "key_facts": ["真凶是管家"]},
        "player_intro": "开场",
        "nodes": [],
        "npcs": [],
    }
    base.update(overrides)
    return base


def test_on_success_becomes_addressable_fact() -> None:
    out, stats = migrate(
        _raw(
            nodes=[
                {
                    "id": "hall",
                    "title": "门厅",
                    "kp_text": "材料",
                    "checks": [{"skill": "侦察", "on_success": "地毯上有半干的泥脚印"}],
                }
            ]
        )
    )
    clue = next(f for f in out["facts"] if f["kind"] == "clue")
    assert clue["text"] == "地毯上有半干的泥脚印"
    assert clue["tier"] == "diegetic"
    assert clue["origin"] == "node:hall.checks[0].on_success"
    assert out["nodes"][0]["checks"][0]["reveals"] == [clue["id"]]
    assert stats["on_success"] == 1


def test_same_on_success_in_two_checks_becomes_one_fact() -> None:
    """🔴 多路径线索：同一条信息经不同技能/地点都能拿到，必须只有一个 id。

    实测依据：神秘渡轮 71 条 on_success 去重后 57 条。
    """
    out, stats = migrate(
        _raw(
            nodes=[
                {
                    "id": "a",
                    "title": "A",
                    "kp_text": "x",
                    "checks": [{"skill": "侦察", "on_success": "同一条线索"}],
                },
                {
                    "id": "b",
                    "title": "B",
                    "kp_text": "x",
                    "checks": [{"skill": "图书馆使用", "on_success": "同一条线索"}],
                },
            ]
        )
    )
    clues = [f for f in out["facts"] if f["kind"] == "clue"]
    assert len(clues) == 1
    assert stats["deduped"] == 1
    assert out["nodes"][0]["checks"][0]["reveals"] == out["nodes"][1]["checks"][0]["reveals"]


def test_sub_nodes_are_traversed() -> None:
    out, _ = migrate(
        _raw(
            nodes=[
                {
                    "id": "study",
                    "title": "书房",
                    "kp_text": "x",
                    "sub_nodes": [
                        {
                            "id": "desk",
                            "title": "书桌",
                            "kp_text": "x",
                            "checks": [{"skill": "侦察", "on_success": "抽屉夹层里有封信"}],
                        }
                    ],
                }
            ]
        )
    )
    assert any(f["origin"] == "node:desk.checks[0].on_success" for f in out["facts"])


def test_npc_notes_drop_keeper_instructions() -> None:
    """NPC 知道的是虚构内的事；给 KP 的操作指导句属元层，不进 npc_knowledge。"""
    out, _ = migrate(
        _raw(
            npcs=[
                {
                    "id": "butler",
                    "name": "管家",
                    "kp_notes": "他昨晚看见有人翻窗进来。如果玩家逼问，让他做一次话术检定",
                }
            ]
        )
    )
    knowledge = next(f for f in out["facts"] if f["kind"] == "npc_knowledge")
    assert "翻窗" in knowledge["text"]
    assert "话术检定" not in knowledge["text"]
    assert out["npcs"][0]["knows"] == [knowledge["id"]]


def test_key_facts_are_meta_and_need_no_reveal_path() -> None:
    """kp_truth 是 KP 的答案纸，不是可直接挣得的条目（exec/15 元层清单）。"""
    out, _ = migrate(_raw())
    truth = next(f for f in out["facts"] if f["kind"] == "truth")
    assert truth["tier"] == "meta"
    # meta 不出现在任何 reveals/knows 里
    assert all(truth["id"] not in (n.get("reveals") or []) for n in out["nodes"])


def test_branch_outcome_is_not_a_fact() -> None:
    """「若 X 则 Y」是状态转移（T），不是可揭示的断言（F）。"""
    out, _ = migrate(
        _raw(
            nodes=[
                {
                    "id": "n",
                    "title": "N",
                    "kp_text": "x",
                    "branches": [{"condition": "若玩家开枪", "outcome": "人影消散"}],
                }
            ]
        )
    )
    assert all("人影消散" not in f["text"] for f in out["facts"])


def test_migration_is_idempotent_and_does_not_lose_custom_fields() -> None:
    raw = _raw(
        nodes=[
            {
                "id": "n",
                "title": "N",
                "kp_text": "x",
                "自定义字段": "必须保留",
                "checks": [{"skill": "侦察", "on_success": "线索"}],
            }
        ]
    )
    once, _ = migrate(raw)
    twice, _ = migrate(once)
    assert json.dumps(once, ensure_ascii=False, sort_keys=True) == json.dumps(
        twice, ensure_ascii=False, sort_keys=True
    )
    assert once["nodes"][0]["自定义字段"] == "必须保留"
    # 输入不被就地修改
    assert "facts" not in raw
