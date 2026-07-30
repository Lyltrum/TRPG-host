"""把已结构化模组迁移到事实寻址 schema（exec/14 P1.3，规格见 exec/15）。

**纯机械、可重放、幂等**：直接在 raw dict 上操作（不走 pydantic，避免丢掉
形状定义之外的自定义字段），每次运行都从头重建 facts / reveals / knows，
跑两遍结果逐字节相同。

## 事实来源与取舍（依据 exec/15 的实测归纳）

- `check.on_success` → fact(clue)，`check.reveals` 引用它。
  95% 单句、中位 12–18 字，本来就是原子断言；且天然绑定"挣得机制"。
- `npc.kp_notes` → 按句分类，非指导语句合成一条 fact(npc_knowledge)，
  `npc.knows` 引用它。NPC 在虚构内知道的常常**超过**玩家，缺的是元层不是信息量。
- `kp_truth.key_facts` → fact(truth, **tier=meta**)。与 exec/15 的元层清单一致：
  它是 KP 的答案纸，不是可直接挣得的条目，因此不需要揭开路径。
- `branch.outcome` → **不进 facts**。「若 X 则 Y」是状态转移（属 T），不是
  可揭示的断言（属 F）；全 5 模组仅 10 条。
- `node.kp_text` → **不拆**。句数中位 4、41% 超 5 句，拆句会得约 600 条，
  正是决策②否掉的全量实体化（会把世界变成查表，压死守秘人的即兴空间）。

## 去重

同一模组内正文相同的 `on_success` 合并成一条事实，多个 `reveals` 指过来——
这是 COC 的多路径线索（实测神秘渡轮 71→57），不是数据错误。

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/migrate_facts.py \\
        --in ../模组资料/追书人.structured.json --out ../模组资料/追书人.structured.json

    # 只看会产出什么、不写文件
    .venv/bin/python scripts/module_probe/migrate_facts.py --in <路径> --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_SENTENCE = re.compile(r"[。！？；\n]")

#: 主持指导语信号：出现这些词的句子是给 KP 的操作指导（元层），不是 NPC
#: 在虚构内知道的事。实测 5 模组 53 句里命中 11 句（21%）。
_INSTRUCTION = re.compile(
    r"(玩家|调查员|PC|守秘人|KP|主持人|骰|检定|若|如果.*则|可以让|应当让|引导|提示他们)"
)


def _iter_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """与 module_loader 同口径的节点遍历（含 sub_node / sub_nodes）。"""
    out: list[dict[str, Any]] = []
    for node in nodes or []:
        out.append(node)
        if isinstance(node.get("sub_node"), dict):
            out.extend(_iter_nodes([node["sub_node"]]))
        out.extend(_iter_nodes(node.get("sub_nodes") or []))
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


class _FactTable:
    """按正文去重的事实表，id 按登记序生成（`fact-001`…）。"""

    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []
        self._by_text: dict[tuple[str, str], str] = {}

    def add(self, text: str, *, kind: str, tier: str, origin: str) -> str:
        text = text.strip()
        key = (text, tier)
        existing = self._by_text.get(key)
        if existing is not None:
            return existing
        fact_id = f"fact-{len(self.facts) + 1:03d}"
        self._by_text[key] = fact_id
        self.facts.append(
            {"id": fact_id, "text": text, "kind": kind, "tier": tier, "origin": origin}
        )
        return fact_id


def migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """返回 (迁移后的 raw, 统计)。输入不被修改。"""
    out = json.loads(json.dumps(raw, ensure_ascii=False))
    table = _FactTable()
    stats = {"on_success": 0, "npc_knowledge": 0, "key_facts": 0, "deduped": 0}

    # 1) 检定成功产出 —— 事实的主干
    for node in _iter_nodes(out.get("nodes") or []):
        node.pop("reveals", None)
        for i, check in enumerate(node.get("checks") or []):
            check.pop("reveals", None)
            text = (check.get("on_success") or "").strip()
            if not text:
                continue
            before = len(table.facts)
            fact_id = table.add(
                text,
                kind="clue",
                tier="diegetic",
                origin=f"node:{node.get('id')}.checks[{i}].on_success",
            )
            stats["on_success"] += 1
            if len(table.facts) == before:
                stats["deduped"] += 1
            check["reveals"] = [fact_id]

    # 2) NPC 在虚构内知道的 —— 剔掉给 KP 的操作指导句
    for npc in out.get("npcs") or []:
        npc.pop("knows", None)
        diegetic = [s for s in _sentences(npc.get("kp_notes") or "") if not _INSTRUCTION.search(s)]
        if not diegetic:
            continue
        fact_id = table.add(
            "。".join(diegetic) + "。",
            kind="npc_knowledge",
            tier="diegetic",
            origin=f"npc:{npc.get('id')}.kp_notes",
        )
        stats["npc_knowledge"] += 1
        npc["knows"] = [fact_id]

    # 3) 真相层 —— 元层，不可挣得，因此不需要揭开路径
    for i, text in enumerate((out.get("kp_truth") or {}).get("key_facts") or []):
        if not (text or "").strip():
            continue
        table.add(text, kind="truth", tier="meta", origin=f"kp_truth.key_facts[{i}]")
        stats["key_facts"] += 1

    out["facts"] = table.facts
    stats["facts_total"] = len(table.facts)
    return out, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移模组到事实寻址 schema")
    parser.add_argument("--in", dest="src", required=True, type=Path)
    parser.add_argument("--out", dest="dst", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    raw = json.loads(args.src.read_text(encoding="utf-8"))
    migrated, stats = migrate(raw)

    print(f"{args.src.name}")
    print(
        f"  事实 {stats['facts_total']} 条 "
        f"(on_success {stats['on_success']} 处引用、去重合并 {stats['deduped']} 处；"
        f"npc_knowledge {stats['npc_knowledge']}；key_facts(meta) {stats['key_facts']})"
    )

    if args.dry_run:
        return 0
    dst = args.dst or args.src
    dst.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已写入 {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
