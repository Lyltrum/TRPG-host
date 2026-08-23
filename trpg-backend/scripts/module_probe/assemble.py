"""预处理管线 · 组装层 + 校验闭环（路线 4a/B + 4b 薄骨架）。

把探针产物（裸抽取 + 关系）组装成 ScenarioModule 结构化 JSON。
4b 起目标 schema 含 exits/contains/sub_nodes/forms/visibility_pairs
（见 docs/keeper-design/exec/07）。

阶段（窄合同多次调用，不压成一次干所有事）：
  1. 实体归组 —— 片段 id → 归宿（node/npc/ending/agenda/kp_truth/…）
  2. 实体成形 —— 按归宿合成 node/npc/ending/agenda 对象
  3. 顶层字段 —— meta / kp_truth / player_intro / opening / kp_guidance
  3b. 密级配对 —— visibility_pairs（启发式 + 可选 LLM 补洞）
  然后机械校验（含 4b 引用闭合）+ 内容保全软项；自修 ≤2 次。

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/assemble.py \\
        --extract ../模组资料/科比特先生.裸抽取.json \\
        --relations-pass1 ../模组资料/科比特先生.关系-pass1.json \\
        --relations-pass2 ../模组资料/科比特先生.关系-pass2.json \\
        --source-txt ../模组资料/科比特先生.txt \\
        --example ../模组资料/追书人.structured.json

产物（默认与 extract 同目录，gitignored）：
  *.structured.json  *.组装中间态.json  *.校验报告.json
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from app.core.llm_tape import TapedSyncClient, build_sync_llm_client

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# 保证能 import app.*
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from migrate_facts import migrate as apply_fact_addressing  # noqa: E402
from parallel import run_parallel  # noqa: E402
from probe import (  # noqa: E402
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    load_api_key,
    read_numbered_lines,
)
from validate_module import (  # noqa: E402
    ENCOUNTER_KIND,
    ValidationReport,
    build_entity_anchors,
    count_out_of_scope,
    normalize_and_prune_checks,
    render_skill_whitelist,
    validate_assembled,
)

MAX_RETRIES = 3
TEMPERATURE = 0.2
# 自修：校验失败后喂回 LLM 重试的次数上限
MAX_REPAIR = 2

# DeepSeek chat 公开价目（元/百万 token，约值，仅用于汇报估算）
_PRICE_INPUT_PER_M = 1.0
_PRICE_OUTPUT_PER_M = 2.0

# ── 目标 schema 说明（给 LLM，与 module_loader.py 逐字对齐）──────────────
TARGET_SCHEMA_DOC = """\
目标 JSON 形状（ScenarioModule，必须严格对齐）：
{
  "meta": {
    "id": "英文短 id",
    "title": "模组标题",
    "era": "可选",
    "tone": "可选",
    "designed_players": "可选",
    "multi_player_note": "可选"
  },
  "kp_truth": {
    "summary": "KP 绝密真相摘要",
    "key_facts": ["关键事实1", "关键事实2"]
  },
  "player_intro": "唯一直接念给玩家的开场介绍（只能用玩家可见信息）",
  "opening": {
    "scene": "可选场景名",
    "script": "可念给玩家的开场脚本",
    "kp_notes": "可选，KP 备注"
  },
  "agenda": [
    {
      "id": "英文短 id",
      "title": "可选",
      "trigger": "自由文本触发条件（不要枚举）",
      "kp_text": "触发后 KP 应推进的内容",
      "effects": ["可选效果说明"],
      "once": true
    }
  ],
  "nodes": [
    {
      "id": "英文短 id",
      "title": "节点标题",
      "kind": "可选：location|encounter|clue|item|event|…（encounter=遭遇/对抗/战斗那一幕）",
      "kp_text": "KP 视角完整信息（可含真相）",
      "public_text": "可选，挣得后可念给玩家的摘要",
      "checks": [
        {
          "skill": "中文技能名（COC7 可解析：侦察/聆听/图书馆使用/话术/格斗：斗殴…）",
          "difficulty": "可选，如 普通/困难",
          "on_success": "可选",
          "on_failure": "可选",
          "on_fumble": "可选",
          "prerequisite": "可选"
        }
      ],
      "branches": [
        {"condition": "条件", "outcome": "结果", "then": [{"condition":"…","outcome":"…"}]}
      ],
      "sub_node": null,
      "sub_nodes": [
        {
          "id": "子节点英文短 id",
          "title": "物件或子区域",
          "kind": "item",
          "kp_text": "可寻址细节",
          "checks": [],
          "branches": [],
          "sub_nodes": [],
          "leads_to": [],
          "exits": [],
          "contains": []
        }
      ],
      "exits": ["空间邻接的 node id"],
      "contains": ["被包含的 node/sub 节点 id"],
      "leads_to": ["情节/因果推进的 node id"]
    }
  ],
  "npcs": [
    {
      "id": "英文短 id",
      "name": "显示名",
      "role": "可选",
      "kp_notes": "KP 笔记：**只写这个人/怪物是谁、怎么演**（身份、性格、动机、说话方式、\
藏着什么）。不写遭遇/交战/撤退/战后那几幕——那些各自是 nodes[] 里 kind=\"encounter\" 的节点",
      "public_text": "可选公开摘要",
      "stats": {"STR": 50, "HP": 10},
      "forms": [
        {"id": "form-id", "name": "形态名", "notes": "可选", "stats": {"HP": 12}}
      ],
      "same_as": ["同一实体的其它 npc id"]
    }
  ],
  "endings": [
    {
      "id": "英文短 id",
      "title": "标题",
      "condition": "可选叙述条件",
      "trigger": "可选可判定触发描述",
      "text": "结局文本"
    }
  ],
  "kp_guidance": {"键": "自由文本指引"},
  "visibility_pairs": [
    {
      "id": "pair-id",
      "public_ref": "公开侧 node 或 npc id",
      "secret_ref": "真相侧 node 或 npc id",
      "note": "可选说明"
    }
  ]
}

图边口诀：exits=空间邻接；contains=包含层级；leads_to=情节/因果推进。三者引用的 id 必须存在。
"""


@dataclass
class CallStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    stage_logs: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)
    # 阶段 2 各实体并行时会同时记账；`+=` 是读-改-写，不是原子的。
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    #: label → 已经用过几次。见 `tape_key_for`。
    label_seq: dict[str, int] = field(default_factory=dict)

    def estimate_cost_cny(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * _PRICE_INPUT_PER_M
            + self.completion_tokens / 1_000_000 * _PRICE_OUTPUT_PER_M
        )


def tape_key_for(stats: CallStats, label: str) -> str:
    """磁带子键。**`label` 本身不够用**——自修会回灌阶段 1 再重跑阶段 2/3，
    于是 `stage2.node:xxx` 在一次跑里出现好几遍。直接拿它当 key，第二轮会
    覆盖第一轮的录音，回放时第二轮拿到第一轮的响应，而且悄无声息。

    加出现序号就够了：回放时控制流由录好的响应决定，是确定性的，所以"第 N 次
    用到这个 label"两次跑一定对得上。并行组内每个 label 只出现一次，所以线程
    顺序也影响不到它。
    """
    with stats.lock:
        n = stats.label_seq.get(label, 0) + 1
        stats.label_seq[label] = n
    return label if n == 1 else f"{label}#{n}"


def _chat_json(
    client: TapedSyncClient,
    *,
    system: str,
    user: str,
    temperature: float,
    stats: CallStats,
    label: str,
) -> dict[str, Any]:
    # 🔴 键在重试循环**外面**算：重试是同一次逻辑调用，不能拿到两个不同的键。
    tape_key = tape_key_for(stats, label)
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            t0 = time.perf_counter()
            response = client.chat.completions.create(
                tape_kind="module_assemble",
                tape_key=tape_key,
                model=DEEPSEEK_MODEL,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            elapsed = time.perf_counter() - t0
            usage = response.usage
            pt = ct = tt = 0
            with stats.lock:
                stats.calls += 1
                if usage is not None:
                    pt = usage.prompt_tokens or 0
                    ct = usage.completion_tokens or 0
                    tt = usage.total_tokens or 0
                    stats.prompt_tokens += pt
                    stats.completion_tokens += ct
                    stats.total_tokens += tt
            raw = response.choices[0].message.content or ""
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"响应不是 JSON 对象：{type(data).__name__}")
            stats.stage_logs.append(
                {
                    "label": label,
                    "elapsed_s": round(elapsed, 2),
                    "attempt": attempt,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": tt,
                }
            )
            print(
                f"  {label}: ok ({elapsed:.1f}s, attempt {attempt}, tokens in={pt} out={ct})",
                flush=True,
            )
            if attempt > 1:
                stats.retries.append(
                    {
                        "label": label,
                        "attempts": attempt,
                        "reason": str(last_err) if last_err else "retry",
                    }
                )
            return data
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(
                f"  {label}: attempt {attempt}/{MAX_RETRIES} failed: {exc}",
                flush=True,
            )
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
    stats.failures.append({"label": label, "error": str(last_err)})
    raise RuntimeError(f"{label} 在 {MAX_RETRIES} 次尝试后仍失败: {last_err}")


def _item_body(lines: list[str], item: dict[str, Any]) -> str:
    try:
        a = int(item["line_start"])
        b = int(item["line_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"条目 {item.get('id')!r} 缺少有效行号") from exc
    if a > b:
        a, b = b, a
    a = max(1, a)
    b = min(len(lines), b)
    return "\n".join(lines[ln - 1] for ln in range(a, b + 1))


def merge_relations(
    pass1: list[dict[str, Any]],
    pass2: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并两遍关系：同一无序对保留两条不同描述，去重完全相同的。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for src in (pass1, pass2):
        for r in src:
            a = str(r.get("item_a") or "").strip()
            b = str(r.get("item_b") or "").strip()
            rel = str(r.get("relation") or "").strip()
            if not a or not b or not rel:
                continue
            key = (min(a, b), max(a, b), rel)
            if key in seen:
                continue
            seen.add(key)
            out.append({"item_a": a, "item_b": b, "relation": rel})
    return out


def format_item_catalog(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for it in items:
        lines.append(f"- id: {it.get('id')}")
        lines.append(f"  title: {it.get('title') or ''}")
        lines.append(f"  what_kind_of_thing: {it.get('what_kind_of_thing') or ''}")
        lines.append(f"  audience: {it.get('audience') or ''}")
        # 🔴 枚举跟自由文本一起给：自由文本有细节（"守秘人笔记部分"），枚举是
        # 下游代码唯一认的东西。只给自由文本，归组就会像真机那次一样把一个
        # audience=KP 的片段归进 opening。
        if it.get("audience_kind"):
            lines.append(f"  audience_kind: {it.get('audience_kind')}（player/kp/both）")
        lines.append(f"  summary: {it.get('summary') or ''}")
        lines.append("")
    return "\n".join(lines)


def format_relations(rels: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for r in rels:
        lines.append(f"- {r['item_a']} ↔ {r['item_b']}: {r['relation']}")
    return "\n".join(lines)


def load_example_skeleton(path: Path | None) -> str:
    """从格式范例抽出**形状骨架**（字段名+类型提示），不把范例正文塞进产物。

    🔴 范例可能是第三方模组（如追书人）；这里只取结构，绝不把范例内容
    写进科比特产物。
    """
    if path is None or not path.is_file():
        return TARGET_SCHEMA_DOC

    raw = json.loads(path.read_text(encoding="utf-8"))

    def skeleton(obj: Any, depth: int = 0) -> Any:
        if depth > 4:
            return "…"
        if isinstance(obj, dict):
            return {k: skeleton(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            if not obj:
                return []
            return [skeleton(obj[0], depth + 1)]
        if isinstance(obj, bool):
            return "bool"
        if isinstance(obj, int | float):
            return "number"
        return "string"

    sk = skeleton(raw)
    return (
        TARGET_SCHEMA_DOC
        + "\n\n格式范例的字段骨架（仅形状，内容已抹去，不要抄范例正文）：\n"
        + json.dumps(sk, ensure_ascii=False, indent=2)
    )


# ── 阶段 1：实体归组 ──────────────────────────────────────────

STAGE1_SYSTEM = """\
你是模组预处理流水线的「实体归组」阶段。

输入：一份模组被切成的片段中的**一批** + 片段间关系。
任务：把**交给你的每个片段**分配到一个归宿，并产出实体清单。
🔴 只处理清单里真实出现的片段 id，**不要凭空补别的 id**。

归宿种类（dest_kind）：
- node —— 调查场景/地点/可到达节点/可发现的线索与行动（会进入 nodes[]）
  **遭遇与对抗也是 node**：跟某个人/怪物正面碰上、伪装被揭穿、打起来、它撤退、
  战后清场——每一段都是玩家**走得到**的一幕，各自一个 node（`kind` 填 "encounter"）。
  判据：**这段材料讲的是"会发生的一件事"，还是"一直在那儿的一个人"？**
  是会发生的事，就归 node，哪怕整段都在讲那只怪物。
- npc —— **这个人/怪物是谁**：身份、性格、动机、怎么演、属性数据（会进入 npcs[]）。
  🔴 **npc 不承载场景。** "遭遇怎么发生""打起来怎么走""什么时候撤退""战后
  怎么收场"是**一幕戏**，归 node。`npcs[].kp_notes` 是个自由文本字段，整幕塞
  进去完全合法、也不会被任何校验拦下——但**谁也走不到它**，那一幕等于没有。
- ending —— **玩家能走到的收束点**（会进入 endings[]）
- agenda —— 不依赖玩家行动、世界自己会推进的事件/时间压力（会进入 agenda[]）
- kp_truth —— 顶层 KP 绝密真相（**仅**纯背景真相/超自然设定）
- kp_guidance —— 顶层 KP 主持指引（节奏、战斗警告、跑偏处理等）
- opening —— 开场脚本素材（薄字段，最多 1 个片段）
- player_intro —— 玩家开场介绍素材（薄字段，最多 1 个片段；仅玩家可见）
- meta —— 模组元信息（薄字段，最多 1 个片段）
- pregen —— **预设调查员卡**（模组附的现成角色：姓名+职业+属性+技能+背景那种）。
  本系统的角色由玩家自己建，这些卡片用不上，但**不许硬塞进别处**——尤其不是
  player_intro（它是薄槽，塞两张就整份作废）。一张卡一个片段，各自归到这里。
- front_matter —— **书本的前后附页**：目录、版权页、出版方 logo、译者说明、
  致谢、页码、封面标题装饰。它们印在书上，但**跟"玩这个模组"没有关系**。
  🔴 **不是兜底垃圾桶**：只要还能想出一个玩家或守秘人会用到它的场合，就不归这里。
  模组简介、人数建议、年代设定 → 那是 meta / kp_guidance，不是 front_matter。

规则：
1. **无孤儿**：每个输入片段 id 必须出现在 assignments 里恰好一次。
2. 被聚合/层级/包含类关系连在一起的片段，应归入同一实体（同一 dest_kind + dest_id）。
3. dest_id 用英文小写连字符；同一实体的多片段共享同一个 dest_id。
4. 分不掉的片段 dest_kind 用 "unassigned"，并在 orphans 里说明原因——禁止静默丢弃。
5. 不要发明清单里没有的片段 id。
6. 只输出 JSON，不要 markdown 围栏。

【硬约束——违反即整批作废】
A. **kp_truth 不是兜底垃圾桶**：只装「背景真相 / 超自然设定」这类**纯背景**片段。
   若片段本身就是结局、事件、线索、地点、NPC、数据块、行动描述——归到对应结构位
  （ending / agenda / node / npc …），**不许因为「和真相有关」就丢进 kp_truth**
  （几乎所有片段都和真相有关）。
B. **薄公开字段各只收 1 个片段**：player_intro / opening / meta **各自最多归入 1 个**
   片段——那个真正「公开开场框架 / 元信息」的片段。
   - meta：只放**一个**片段——那个说清楚"这是什么模组、几个人玩、什么年代"的。
     目录 / 版权页 / logo / 译者说明 / 页码一律进 **front_matter**，它们看起来像
     meta，但塞进来就超上限、整份作废。
   - opening：只放**玩家可见**的开场场景脚本（通常 1 个 initial-scene 类）。
   - player_intro：只放**玩家可见**的开场介绍框架（若 opening 已覆盖开场，player_intro
     可空——即 0 个片段也合法，不要硬塞）。
   - 模组简介若 audience 是守密人/KP → 进 **kp_guidance**，不要塞 opening/meta。
   **调查线索**（报纸剪报、证词、现场发现、可挣得的情报）、**调查行动**、**地点描述**
   一律进 **node**（在某个调查节点里被发现），**禁止**塞进 player_intro。
   **预设调查员卡**进 **pregen**，一张一条——它们确实是玩家可见的，但不是开场介绍。
   player_intro+opening+meta 合计 ≤ 3。
C. **结局与时间压力要建实体**（强制，不可吞进 kp_truth）：
   - what_kind_of_thing 指示「结局 / 结尾 / 结束」→ dest_kind=**ending**，建 ending 实体；
     🔴 **但先分清是"收束点"还是"收尾材料"**。ending 只装**玩家的行动能触发的
     结果**（"如果调查员烧掉那本书，则…"）。下面这些**不是** ending，一律进
     **kp_guidance**：
       · 尾声 / 后日谈 / 战役延续的可能性 / 续作钩子
       · 奖励表 / 理智值奖惩清单 / 经验结算
       · 「后续冒险选项」这类给守秘人的续跑建议
     判据一句话：**玩家能不能靠做某件事走到它？** 不能，就不是 ending。
     🔴 **endings[] 可以为空。** 开放收尾的模组本来就没有可到达的收束点，如实
     留空即可，**不要为了填满它而把尾声改写成结局**——伪造出来的那条会一路骗
     到试跑判据，让整个模组永远"收束不了"。
   - 指示「当前事件 / 行动规律 / 时间压力 / 今晚 / 期限」等时间驱动 → dest_kind=**agenda**，
     建 agenda 条目（trigger 用自由文本）。
   - 可有多条 agenda；至少覆盖所有带上述信号的片段。
D. **利用 audience 辅助归组**：
   - audience 含「绝密 / 守密人 / KP」的片段 **不得** 归到 player_intro 或 opening；
   - 玩家可见的调查线索仍应进 node，不是 player_intro。
E. 🔴 **遭遇 / 对抗 / 战斗 / 撤退 / 战后处理不许归进 npc**（实测踩过：整整一幕
   被压成一段摘要塞进 `npcs[].kp_notes`，13 道校验一条都没响，而那一幕在对局里
   永远不会发生）。
   - 一个怪物同时有「数据」和「怎么遭遇它」两类材料时**必须拆开**：
     属性/攻击/护甲那一段进 **npc**；伪装、揭穿、交战、撤退、战后各自进
     **各自的 node**（`kind`="encounter"），一幕一个 id，不要合成一个。
   - 反过来同样：**别把纯数据块拆成 node**。数据块不是一幕戏。

输出形状：
{
  "entities": [
    {"kind": "node|npc|ending|agenda|…", "id": "…", "title": "…", "item_ids": ["…"]}
  ],
  "assignments": [
    {"item_id": "…", "dest_kind": "…", "dest_id": "…", "reason": "一句话"}
  ],
  "orphans": [{"item_id": "…", "why": "…"}]
}
"""


#: 🔴 **归组分批的批大小**（2026-08-20）。
#:
#: ## 为什么必须分批
#:
#: `stage1` 要求「每个片段 id 必须出现在 assignments 中」，于是**输出长度正比于
#: 片段数**，而 `max_tokens` 是个常数——两者必然在某个模组上相撞。
#:
#: 真机撞上了：一份 241 片段的模组，模型写到 **24,523 字符**仍未写完，JSON 被
#: 截断（`Unterminated string`），自动重试 3 次全部同样失败。历史最大的一份是
#: 116 片段（跑通过），所以阈值落在 116–241 之间。
#:
#: 🔴 **调大 `max_tokens` 不是修法，是把阈值往后挪一格**：输出仍然 ∝ 片段数，
#: 下一份更碎的模组照样撞。隔壁 `relation_probe` 早就是「**总是分批**」
#: （`pipeline.py` 注释：分批是全量的超集，只是慢；导入是后台任务，不在乎慢），
#: 只有归组这步没照做。
#:
#: 取 60：历史成功过的最大是 116，砍一半留足余量；四批之内跑完一份很碎的模组。
STAGE1_BATCH_SIZE = 60


# 🔴 **这里故意不给全局片段简表**（2026-08-20，第一版给了，当场被打脸）。
#
# 第一版照抄 `relation_probe` 的「焦点批 + 全局简表」，并在 user prompt 里写明
# 「本批之外的片段也在这里……**不要**为它们产出 assignment」。真机结果：batch0
# 只有 60 个片段，模型却输出了 24,500 字符 ≈ 241 条 assignment——**它把全部
# 241 条都做了**，跟分批前一样撞上截断。
#
# 根因是 `STAGE1_SYSTEM` 里写着「输入：一份模组已被切成的**全部**片段清单 /
# 把**每个片段**分配到一个归宿」——**两句话打架，system 赢**。这是项目判据
# 「**加了门要回头改被绕过的那句话**」的第三次（前两次是 SAN 豁免）。
#
# 但只改措辞仍然是「请你别做」。改成**让它拿不到**：批外的 id 根本不出现在
# 上下文里，想做也做不出来。同 `access/` 那条「**保密靠拿不到，不是请你别说**」
# ——约束也一样。
#
# 代价是第一批没有全局视野。跨批一致性由「已建实体」承担（见下），而归组**不
# 需要**跨批引用片段 id：它只需要跨批引用**实体** id。这正是它跟关系发现的
# 差别——那边要找跨批的关系对，简表是必需的。


def _format_known_entities(entities: list[dict[str, Any]]) -> str:
    """前几批已经建好的实体。**这是分批与全量唯一的语义差别所在。**

    🔴 关系发现那步各批之间没有数据依赖，可以并行；归组不行——它要**创建实体**，
    两批各跑各的就会给同一个场景建出两个 id（"地下室" / "cellar"），合并时谁也
    认不出它们是一回事。所以这里**串行**，并把已建实体带进下一批。
    """
    if not entities:
        return ""
    lines = [f"- {e.get('kind')}/{e.get('id')}: {e.get('title') or ''}" for e in entities]
    return (
        "\n\n【前面几批已经建好的实体】\n"
        + "\n".join(lines)
        + "\n🔴 本批片段如果属于上面某个已有实体，**直接用它的 id**，不要另起一个新的。"
    )


AUDIENCE_KINDS = ("player", "kp", "both")

AUDIENCE_SYSTEM = """\
你在给模组片段的「受众」分类。

上一步的抽取**故意**让 audience 保持自由文本（"按内容如实写，不要强迫二选一"），
所以它现在什么写法都有：`玩家可见` / `KP绝密` / `守秘人` / `KP（模组真相部分）` /
`玩家可见（守秘人笔记部分为守秘人绝密）`……

你的任务：把每一种写法归到三档之一。

- player —— 玩家读得到的内容（开场白、公开情报、玩家手边资料）
- kp —— 只有主持人能看的内容（真相、谜底、主持指引、失败处理、NPC 底牌）
- both —— 主体玩家可见、但夹着一部分主持人专属（如"玩家可见（守秘人笔记部分绝密）"）

判据：**"如果把这段原样念给玩家听，会不会毁掉悬念？"** 会 → kp。

🔴 拿不准时选 kp。剧透一次就毁掉整局，而多藏一段最多是主持人多讲一句。

输出格式：只输出一个 JSON 对象，形如
{"kinds": {"<原文>": "player|kp|both", ...}}，不要 markdown 围栏。
键必须与给你的原文**逐字相同**。
"""


def translate_audiences(
    client: TapedSyncClient,
    items: list[dict[str, Any]],
    stats: CallStats,
) -> dict[str, str]:
    """把 audience 的自由文本翻译成 `AUDIENCE_KINDS` 里的一档。

    ## 🔴 为什么需要这一层（2026-08-20）

    抽取那头保持自由文本是**对的**（`probe.py`：常见是玩家可见或 KP 绝密，
    "但也可能是别的情况，按内容如实写，不要强迫二选一"）——实测里
    `玩家可见（守秘人笔记部分为守秘人绝密）` 这种复合受众，二选一确实会丢信息。

    **错在下游没有翻译就直接拿它当标识符用。** 原来的
    `_audience_is_keeper_secret` 是个关键词表（"绝密"/"守密人"/"KP"），而实测
    五份模组的取值有 20 多种写法，那张表**同时有漏判和误判**：

    - 漏：代码写的是「守密**人**」，数据里是「守**秘**人」，一字之差全漏；
    - 误：`玩家可见（守秘人笔记部分为守秘人绝密）` 含"绝密"就被整条判成 KP。

    判据在项目里写着：**不要用自由文本当标识符，要么是白名单 id，要么退化成
    同义词打地鼠**。这一层就是那个"翻译成白名单"的步骤。

    ## 一次调用就够

    **按字符串去重**——170 个片段写的都是"玩家可见"，只需要翻译一次。实测五份
    模组去重后各只有 10–20 种写法。

    🔴 **不在这里加规则表兜底**（"含'玩家可见'就直接判 player"）：那会把刚拆掉
    的打地鼠原样搬进来。唯一的例外是**空值**——它没有内容可理解，也不该花一次
    调用。
    """
    raw = sorted({str(it.get("audience") or "").strip() for it in items} - {""})
    if not raw:
        return {}
    user = "请给下面每一种写法分类：\n" + "\n".join(f"- {a}" for a in raw)
    data = _chat_json(
        client,
        system=AUDIENCE_SYSTEM,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label="audience.translate",
    )
    kinds = data.get("kinds") or {}
    out: dict[str, str] = {}
    for a in raw:
        kind = str(kinds.get(a) or "").strip().lower()
        # 🔴 模型没给或给了非法值时**落到 kp**，不是 player。同 prompt 里那条：
        # 剧透一次毁掉整局，多藏一段只是主持人多讲一句。判错的代价不对称。
        out[a] = kind if kind in AUDIENCE_KINDS else "kp"
    return out


def apply_audience_kinds(items: list[dict[str, Any]], kinds: dict[str, str]) -> int:
    """把翻译结果写回片段，返回写了几条。

    空 audience 落 `kp`：没有内容可判断时按最保守的来（同上，代价不对称）。
    """
    written = 0
    for it in items:
        raw = str(it.get("audience") or "").strip()
        it["audience_kind"] = kinds.get(raw, "kp") if raw else "kp"
        written += 1
    return written


#: KP 片段被归进公开槽时，改把它挪到哪个槽。
#:
#: `kp_guidance` 而不是 `kp_truth`：被误归进 opening/player_intro 的通常是
#: 「模组简介」「怎么开场」这类**主持指引**（真机那次正是一个叫 `introduction`
#: 的片段），而 `kp_truth` 是谜底。放错成谜底会让收尾判据把它当成真相侧。
_KP_FALLBACK_SLOT = "kp_guidance"

_PUBLIC_SLOTS = ("player_intro", "opening")


def enforce_audience_slots(
    assignment_map: dict[str, dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """把 `audience_kind == "kp"` 却归进公开槽的片段挪走。返回改了哪几条。

    ## 🔴 为什么这一步必须是代码，不是重试

    真机实测：一个叫 `introduction` 的片段，audience 是 `KP（模组真相部分）`，
    归组模型**每一轮都把它归进 opening**——因为它名字就叫"引言"，那是模型的
    语义直觉，重试三次一模一样。而这一条错误让 `needs_stage1_repair()` 永远为
    真，整个自修被锁死在"整份重吐"那条路上，后面所有窄路一次都没跑过。

    有了 `audience_kind` 之后，**这个判断是纯机械的**：一边是枚举值，一边是
    槽位名，代码全都看得见。判据：**能用代码确定性判断的一律代码强制**。

    ⚠️ 这不代表归组模型可以随便归——它的 prompt 里也拿得到 `audience_kind`
    （`format_item_catalog` 会渲染）。这一步是**兜底**，不是替它做决定。
    """
    moved: list[str] = []
    for iid, info in sorted(assignment_map.items()):
        if str(info.get("dest_kind") or "") not in _PUBLIC_SLOTS:
            continue
        kind = str((items_by_id.get(iid) or {}).get("audience_kind") or "").strip().lower()
        if kind != "kp":
            continue
        was = info["dest_kind"]
        info["dest_kind"] = _KP_FALLBACK_SLOT
        info["dest_id"] = ""
        info["reason"] = f"audience_kind=kp，自 {was} 移入 {_KP_FALLBACK_SLOT}（代码强制）"
        moved.append(f"{iid}: {was} → {_KP_FALLBACK_SLOT}")
    return moved


def stage1_group(
    client: TapedSyncClient,
    items: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    stats: CallStats,
    *,
    repair_notes: str = "",
    batch_size: int = STAGE1_BATCH_SIZE,
) -> dict[str, Any]:
    """把每个片段分配到一个归宿，并产出实体清单。**总是分批**（理由见
    `STAGE1_BATCH_SIZE`）；片段少时也就是一批，与分批前逐字同形。"""
    valid_ids = {str(it["id"]) for it in items if it.get("id")}
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    merged_assignments: list[dict[str, Any]] = []
    merged_entities: list[dict[str, Any]] = []
    entity_by_id: dict[str, dict[str, Any]] = {}

    def _run_batch(focus: list[dict[str, Any]], label: str) -> dict[str, Any]:
        """跑一批；**输出撑爆就对半拆，直到跑通或拆到单个片段**。

        ## 🔴 为什么不是"再调一次批大小"

        第一版按**片段数**切（60 一批），坨子岛 218 片段跑通了，秘鲁序章 161
        片段却在 batch0 就截断——**片段数更少反而更长**：

            坨子岛：45,011 字符 / 218 片段 = 206 字符/片段
            秘鲁：  58,269 字符 / 161 片段 = 362 字符/片段（1.76 倍）

        片段的"大小"在模组之间差着一倍多，**按个数切就是拿错了度量**。而按字符
        数切也只是换一个猜得准一点的阈值——输出长度是模型决定的，输入长度只是
        个相关量，下一份模组照样能找到反例。

        自动对半拆不需要任何阈值：**撞上了就拆细，拆到跑通为止**。这条线的定位
        是「只能成功不能失败」，而这正是那个"更窄的修法"。

        代价是撞上时多花几次调用。可以接受——它只在真的撞上时才发生。
        """
        try:
            return _chat_json(
                client,
                system=STAGE1_SYSTEM,
                user=(
                    f"【本批要归组的片段，共 {len(focus)} 个】\n"
                    + format_item_catalog(focus)
                    + "\n【片段间关系】\n"
                    + format_relations(rels)
                    + _format_known_entities(merged_entities)
                    + "\n\n请产出 entities + assignments + orphans。"
                    + "**本批**的每个片段 id 必须出现在 assignments 中。"
                    + (f"\n\n【上轮校验/归组问题，请修正】\n{repair_notes}" if repair_notes else "")
                ),
                temperature=TEMPERATURE,
                stats=stats,
                # 🔴 label 带批号：磁带按 label 取键，同名会让几批互相覆盖。
                label=label,
            )
        except RuntimeError:
            if len(focus) <= 1:
                # 单个片段都吐不完整，那不是长度问题，如实往上抛
                raise
            mid = len(focus) // 2
            print(
                f"  ↯ {label} 输出撑爆，拆成 {mid} + {len(focus) - mid} 重跑",
                flush=True,
            )
            left = _run_batch(focus[:mid], f"{label}.a")
            # 🔴 先并进 merged_entities 再跑右半——右半要看得见左半刚建的实体，
            # 否则同一个场景会被两半各建一个 id（分批那条判据在拆分时同样成立）。
            _absorb(left)
            return _merge_batch_data(left, _run_batch(focus[mid:], f"{label}.b"))

    def _merge_batch_data(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        """把两半的产出并成一份，形状与单批一致。"""
        return {
            "entities": (a.get("entities") or []) + (b.get("entities") or []),
            "assignments": (a.get("assignments") or []) + (b.get("assignments") or []),
            "orphans": (a.get("orphans") or []) + (b.get("orphans") or []),
        }

    def _absorb(data: dict[str, Any]) -> None:
        """把一批的实体并进已建清单（拆分时左半要先并，右半才看得见）。"""
        for ent in data.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            eid = str(ent.get("id") or "").strip()
            if eid and eid not in entity_by_id:
                entity_by_id[eid] = ent
                merged_entities.append(ent)

    for bi, focus in enumerate(batches):
        data = _run_batch(focus, "stage1.group" if len(batches) == 1 else f"stage1.group:batch{bi}")
        for row in data.get("assignments") or []:
            if isinstance(row, dict):
                merged_assignments.append(row)
        for ent in data.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            eid = str(ent.get("id") or "").strip()
            if not eid:
                continue
            known = entity_by_id.get(eid)
            if known is None:
                entity_by_id[eid] = ent
                merged_entities.append(ent)
                continue
            # 🔴 同一个实体被两批各归了一部分片段——**合并 item_ids，不是丢掉后来的**。
            # 直接 `continue` 会让后面几批分给它的片段在 `entities[].item_ids` 里
            # 消失，而那个字段有下游消费方（stage2 渲染「包含片段」时读它）。
            merged = list(known.get("item_ids") or [])
            for iid in ent.get("item_ids") or []:
                if iid not in merged:
                    merged.append(iid)
            known["item_ids"] = merged

    return _normalize_stage1(
        {"entities": merged_entities, "assignments": merged_assignments}, valid_ids
    )


def _normalize_stage1(data: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
    assignments_raw = data.get("assignments") or []
    if not isinstance(assignments_raw, list):
        raise ValueError("stage1 assignments 不是数组")

    assignment_map: dict[str, dict[str, Any]] = {}
    for row in assignments_raw:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("item_id") or "").strip()
        if not iid:
            continue
        assignment_map[iid] = {
            "dest_kind": str(row.get("dest_kind") or "").strip() or "unassigned",
            "dest_id": str(row.get("dest_id") or "").strip(),
            "reason": str(row.get("reason") or "").strip(),
        }

    # 补洞：LLM 漏掉的片段强制标 unassigned，保证映射键完整（逆投影①）
    for iid in sorted(valid_ids):
        if iid not in assignment_map:
            assignment_map[iid] = {
                "dest_kind": "unassigned",
                "dest_id": "",
                "reason": "阶段1未分配，机械补为 unassigned",
            }

    entities_raw = data.get("entities") or []
    entities: list[dict[str, Any]] = []
    if isinstance(entities_raw, list):
        for ent in entities_raw:
            if not isinstance(ent, dict):
                continue
            kind = str(ent.get("kind") or "").strip()
            eid = str(ent.get("id") or "").strip()
            if not kind or not eid:
                continue
            item_ids = [
                str(x).strip() for x in (ent.get("item_ids") or []) if str(x).strip() in valid_ids
            ]
            entities.append(
                {
                    "kind": kind,
                    "id": eid,
                    "title": str(ent.get("title") or "").strip(),
                    "item_ids": item_ids,
                }
            )

    # 若 entities 空或缺，从 assignment_map 反推
    if not entities:
        buckets: dict[tuple[str, str], list[str]] = {}
        for iid, info in assignment_map.items():
            kind = info["dest_kind"]
            dest_id = info["dest_id"] or iid
            if kind in ("unassigned",):
                continue
            buckets.setdefault((kind, dest_id), []).append(iid)
        for (kind, dest_id), iids in buckets.items():
            entities.append(
                {
                    "kind": kind,
                    "id": dest_id,
                    "title": dest_id,
                    "item_ids": iids,
                }
            )

    # 🔴 **让两份映射自洽**（2026-08-20，真机 trace 失败逼出来的）。
    #
    # `entities[].item_ids` 与 `assignments` 表达的是**同一个映射的两个方向**
    # （实体→片段 / 片段→实体），而模型的输出经常在两处对不上：真机上
    # `node:third-day-trap-su ← 1 items` 明明认领了一个片段，`assignment_map`
    # 里却没有任何片段指向它 ⇒ trace 判它「没有溯源锚点」，而三轮自修都修不掉
    # ——`repair#N.entity` 重吐的是实体内容，动不了 assignment_map。
    #
    # 修的方向是**单向的**：只补 `assignment_map` 里还没主的片段（缺失或
    # unassigned）。已经归给别人的**不抢**——那是一个片段两个归宿的冲突，
    # 属于真正的归组错误，该让校验报出来，不该在这里悄悄改掉。
    for ent in entities:
        eid = str(ent.get("id") or "")
        kind = str(ent.get("kind") or "")
        if not eid or not kind:
            continue
        for iid in ent.get("item_ids") or []:
            info = assignment_map.get(iid)
            if info is None or info["dest_kind"] == "unassigned":
                assignment_map[iid] = {
                    "dest_kind": kind,
                    "dest_id": eid,
                    "reason": f"由 entities[{eid}].item_ids 反向补齐（两份映射对不上）",
                }

    orphans_raw = data.get("orphans") or []
    orphans: list[dict[str, Any]] = []
    if isinstance(orphans_raw, list):
        for o in orphans_raw:
            if isinstance(o, dict) and o.get("item_id"):
                orphans.append(
                    {
                        "item_id": str(o["item_id"]),
                        "why": str(o.get("why") or ""),
                    }
                )
    for iid, info in assignment_map.items():
        if info["dest_kind"] == "unassigned" and not any(o["item_id"] == iid for o in orphans):
            orphans.append({"item_id": iid, "why": info.get("reason") or "unassigned"})

    return {
        "entities": entities,
        "assignments": [{"item_id": k, **v} for k, v in sorted(assignment_map.items())],
        "assignment_map": assignment_map,
        "orphans": orphans,
    }


# ── 阶段 2：实体成形 ──────────────────────────────────────────

STAGE2_NODE_SYSTEM = """\
你是模组预处理流水线的「实体成形·节点」阶段。

任务：根据归到同一 node 的片段原文，合成 ScenarioModule.nodes[] 里的对象。
严格遵守目标 schema。

检定点 checks[] 每条填三个字段：
- skill_ids：从下面那张白名单里挑，**逐字照抄 id**。多个 id 表示「任一命中即可」\
（原文写成「话术/魅惑」这种就填多个）。原文的写法不在表里时，挑语义最接近的那个 id；\
实在找不到就**不要写这条检定**，别自己造一个名字。
- kind："skill"；理智检定填 "san" 且 skill_ids 留空。
- skill：白名单里对应的中文名，逐字照抄。

图边必须分开填（只能引用已知 node id）：
- exits：空间上相邻的房间/地点
- contains：本节点内的物件/子区域 id（若那些 id 也在已知 node 列表中）
- leads_to：情节或因果上推进到的下一调查点（不是房间邻接表）
大段地点描述里若有可独立寻址的物件/子区，写入 sub_nodes[]（带独立 id），\
不要只堆在 kp_text。

本节点是**遭遇/对抗**（跟某个人或怪物正面碰上、伪装被揭穿、交战、它撤退、
战后清场）时，`kind` 填 "encounter"，并把它接进图里——**遭遇必须走得到**：
用 leads_to 从触发它的那个调查点指过来，或用 exits 接上它发生的地点。
交战规则（谁先手、命中什么后果、什么条件下撤退）写进 kp_text 与 checks，
**不要**指望 npcs[].kp_notes 去承载它。
有玩家可见摘要时填 public_text。
kp_text 可含 KP 绝密信息。不要发明原文没有的关键事实。

只输出 JSON：{"nodes": [ ... ]}
"""

STAGE2_NPC_SYSTEM = """\
你是模组预处理流水线的「实体成形·NPC」阶段。

任务：根据归到同一 npc 的片段，合成 npcs[] 对象。
stats 用自由字典（可含 STR/CON/SIZ/DEX/INT/POW/HP/DB/护甲等原文数据）。
战斗/属性数据优先写入 stats 或 forms[].stats，不要只留在旁路叙述。

🔴 **kp_notes 只写「这个人/怪物是谁、怎么演」**：身份、性格、动机、说话方式、
藏着什么秘密。**不写一幕戏**——遭遇怎么开始、伪装怎么被揭穿、交战怎么走、
何时撤退、战后怎么收场，这些都属于 nodes[]（kind="encounter"），不属于这里。
材料里混进了那类内容时，kp_notes 只留一句「怎么演」，**其余不要改写进来**
（它们已经或将由节点阶段承接；重复写进 kp_notes 只会制造两份说法）。
判据：**kp_notes 是人物小传，不是剧本。**
同一实体的多形态（成体/幼体等）写入 forms[]；若公开形象与秘密身份是两行 npc，\
在 same_as 里写上对方 id（若已知）。
可填 public_text 作公开摘要。不要发明原文没有的数值。

只输出 JSON：{"npcs": [ ... ]}
"""

STAGE2_ENDING_SYSTEM = """\
你是模组预处理流水线的「实体成形·结局」阶段。

任务：根据结局相关片段合成 endings[]。
每条需要 id/title/text；condition 与 trigger 能从原文抽出就填，否则可省略。

只输出 JSON：{"endings": [ ... ]}
"""

STAGE2_AGENDA_SYSTEM = """\
你是模组预处理流水线的「实体成形·议程」阶段。

任务：根据议程相关片段合成 agenda[]。
trigger 必须是自由文本（描述何时触发），不要用枚举。
once 默认 true。
若原文写了触发后的可执行后果（路线关闭、时间推进、场景变化等），填入 effects[]，\
不要总给空列表。

只输出 JSON：{"agenda": [ ... ]}
"""


def _bundle_entity_materials(
    entity: dict[str, Any],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    rels: list[dict[str, Any]],
) -> str:
    parts: list[str] = [
        f"实体 kind={entity['kind']} id={entity['id']} title={entity.get('title') or ''}",
        f"包含片段：{', '.join(entity.get('item_ids') or [])}",
        "",
    ]
    id_set = set(entity.get("item_ids") or [])
    for iid in entity.get("item_ids") or []:
        it = items_by_id.get(iid)
        if it is None:
            continue
        body = _item_body(lines, it)
        parts.append(f"### 片段 {iid}")
        parts.append(f"title: {it.get('title')}")
        parts.append(f"audience: {it.get('audience')}")
        parts.append(f"kind: {it.get('what_kind_of_thing')}")
        parts.append(f"summary: {it.get('summary')}")
        parts.append("原文：")
        parts.append(body)
        parts.append("")
    # 相关关系
    related = [r for r in rels if r["item_a"] in id_set or r["item_b"] in id_set]
    if related:
        parts.append("### 相关关系")
        parts.append(format_relations(related))
    return "\n".join(parts)


def stage2_form_kind(
    client: TapedSyncClient,
    *,
    kind: str,
    entities: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    rels: list[dict[str, Any]],
    known_node_ids: list[str],
    stats: CallStats,
    schema_doc: str,
    repair_notes: str = "",
) -> list[dict[str, Any]]:
    targets = [e for e in entities if e.get("kind") == kind]
    if not targets:
        print(f"  stage2.{kind}: 无实体，跳过", flush=True)
        return []

    system = {
        "node": STAGE2_NODE_SYSTEM,
        "npc": STAGE2_NPC_SYSTEM,
        "ending": STAGE2_ENDING_SYSTEM,
        "agenda": STAGE2_AGENDA_SYSTEM,
    }.get(kind)
    if system is None:
        print(f"  stage2.{kind}: 非成形种类，跳过（留给阶段3）", flush=True)
        return []
    if kind == "node":
        # 🔴 白名单跟 `check_skills` 用的是同一份 id 表——让模型**在写的时候**
        # 就从 enum 里挑，而不是事后拿别名表去猜它写了什么。
        system += "\n" + render_skill_whitelist()

    # 按实体逐个调用——窄合同，避免一次塞太多
    results: list[dict[str, Any]] = []
    key_name = {
        "node": "nodes",
        "npc": "npcs",
        "ending": "endings",
        "agenda": "agenda",
    }[kind]

    # 每个实体一次窄合同调用，彼此无依赖（`known_node_ids` 是上游传进来的定值）。
    def _one_entity(ent: dict[str, Any]) -> dict[str, Any]:
        materials = _bundle_entity_materials(ent, items_by_id, lines, rels)
        user = (
            f"{schema_doc}\n\n"
            "【已知全部 node id（exits/contains/leads_to 只能用这些）】\n"
            f"{', '.join(known_node_ids) or '（尚未固定，请用实体 id 列表）'}\n\n"
            f"【本实体材料】\n{materials}\n\n"
            f"请只产出这一个 {kind} 对象，放在 {key_name} 数组里（长度 1）。"
            f"id 必须是 {ent['id']!r}。"
        )
        if repair_notes:
            user += f"\n\n【校验问题请一并修正】\n{repair_notes}"
        return _chat_json(
            client,
            system=system,
            user=user,
            temperature=TEMPERATURE,
            stats=stats,
            label=f"stage2.{kind}:{ent['id']}",
        )

    # run_parallel 按输入顺序返回，所以 results 的顺序与串行时一致。
    for ent, data in zip(
        targets, run_parallel([partial(_one_entity, e) for e in targets]), strict=True
    ):
        arr = data.get(key_name)
        if isinstance(arr, list):
            for obj in arr:
                if isinstance(obj, dict):
                    # 强制 id
                    obj["id"] = ent["id"]
                    if not obj.get("title"):
                        obj["title"] = ent.get("title") or ent["id"]
                    results.append(obj)
        elif isinstance(data.get(kind), dict):
            obj = data[kind]
            obj["id"] = ent["id"]
            results.append(obj)
        else:
            # 容错：响应本身就是实体
            if "id" in data or "kp_text" in data or "name" in data or "text" in data:
                data["id"] = ent["id"]
                results.append(data)
            else:
                print(f"    warn: stage2.{kind}:{ent['id']} 无法解析，跳过", flush=True)
    return results


# ── 阶段 3：顶层字段 ──────────────────────────────────────────

STAGE3_SYSTEM = """\
你是模组预处理流水线的「顶层字段」阶段。

任务：产出 meta / kp_truth / player_intro / opening / kp_guidance。

硬约束：
1. player_intro 与 opening.script **只能**使用玩家可见信息，禁止写入 KP 绝密真相。
2. kp_truth.summary 与 key_facts 放绝密；key_facts 里的措辞不要出现在 player_intro/opening.script。
3. kp_guidance 是 KP 主持用自由文本字典（键用英文蛇形或短中文均可）。
4. 不要输出 nodes/npcs/endings/agenda（那些已在其它阶段完成）。
5. **薄字段内容保全**：player_intro / opening 各自通常只对应 1 个归组片段——
   只用「标记为 player_intro / opening 的那一个片段」写它们的正文。
   **不要**把调查线索、报纸、行动描写并进 player_intro（那些应已在 nodes 里）。
6. kp_truth 只用标记为 kp_truth 的纯背景片段；
   不要把结局/议程材料写进 key_facts 代替 endings/agenda。
7. 只输出 JSON：
{"meta":{…},"kp_truth":{…},"player_intro":"…","opening":{…},"kp_guidance":{…}}
"""


def stage3_toplevel(
    client: TapedSyncClient,
    *,
    items: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    stage1: dict[str, Any],
    schema_doc: str,
    stats: CallStats,
    module_title_hint: str,
    repair_notes: str = "",
) -> dict[str, Any]:
    # 收集顶层相关材料
    top_kinds = {
        "kp_truth",
        "kp_guidance",
        "opening",
        "player_intro",
        "meta",
        "unassigned",
    }
    amap: dict[str, dict[str, Any]] = stage1["assignment_map"]
    relevant_ids = [iid for iid, info in amap.items() if info.get("dest_kind") in top_kinds]
    # 也带上 audience 标明玩家可见 / 绝密 的摘要，便于分区
    parts: list[str] = [f"模组标题提示：{module_title_hint}", ""]
    for iid in relevant_ids:
        it = items_by_id.get(iid)
        if it is None:
            continue
        info = amap[iid]
        body = _item_body(lines, it)
        # 正文可能很长：顶层阶段截断单片段，避免爆上下文
        if len(body) > 2500:
            body = body[:2500] + "\n…(截断)"
        parts.append(f"### {iid} → {info.get('dest_kind')}/{info.get('dest_id')}")
        parts.append(f"audience: {it.get('audience')}")
        parts.append(f"summary: {it.get('summary')}")
        parts.append(body)
        parts.append("")

    # 额外：全部片段的 id/title/audience/summary 简表，防漏
    parts.append("### 全部片段简表（供补漏）")
    for it in items:
        parts.append(
            f"- {it.get('id')}: [{it.get('audience')}] {it.get('title')} — "
            f"{str(it.get('summary') or '')[:80]}"
        )

    user = (
        f"{schema_doc}\n\n"
        f"【顶层相关材料】\n" + "\n".join(parts) + "\n\n"
        "请产出 meta / kp_truth / player_intro / opening / kp_guidance。"
    )
    if repair_notes:
        user += f"\n\n【校验问题请修正——尤其注意不泄密】\n{repair_notes}"

    data = _chat_json(
        client,
        system=STAGE3_SYSTEM,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label="stage3.toplevel",
    )
    # 最低限度字段兜底
    raw_meta = data.get("meta")
    meta: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    if not meta.get("id"):
        meta["id"] = "assembled-module"
    if not meta.get("title"):
        meta["title"] = module_title_hint
    raw_truth = data.get("kp_truth")
    kp_truth: dict[str, Any] = dict(raw_truth) if isinstance(raw_truth, dict) else {}
    if not kp_truth.get("summary"):
        kp_truth["summary"] = "（组装层未给出摘要）"
    if not isinstance(kp_truth.get("key_facts"), list):
        kp_truth["key_facts"] = []
    player_intro = str(data.get("player_intro") or "").strip() or "（开场介绍待补）"
    raw_opening = data.get("opening")
    opening: dict[str, Any] | None = dict(raw_opening) if isinstance(raw_opening, dict) else None
    if opening is not None and not opening.get("script"):
        opening["script"] = player_intro
    raw_guidance = data.get("kp_guidance")
    guidance_src: dict[str, Any] = dict(raw_guidance) if isinstance(raw_guidance, dict) else {}
    kp_guidance = {str(k): str(v) for k, v in guidance_src.items()}

    return {
        "meta": meta,
        "kp_truth": kp_truth,
        "player_intro": player_intro,
        "opening": opening,
        "kp_guidance": kp_guidance,
    }


# ── 组装 + 自修 ──────────────────────────────────────────────


def _walk_node_dicts(nodes: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        out.append(n)
        sub = n.get("sub_node")
        if isinstance(sub, dict):
            out.extend(_walk_node_dicts([sub]))
        subs = n.get("sub_nodes")
        if isinstance(subs, list):
            out.extend(_walk_node_dicts([c for c in subs if isinstance(c, dict)]))
    return out


def mechanical_sanitize_module(module: dict[str, Any]) -> list[str]:
    """校验前的机械修补：去重 id、剪悬空边、清非法 same_as/pairs。

    整份 JSON 自修在大体量模组上常因输出截断失败；引用类错误应优先机械修。
    返回人类可读的修补日志。
    """
    notes: list[str] = []
    nodes = module.get("nodes")
    if not isinstance(nodes, list):
        return notes

    # 1) 扁平遍历，重命名重复 id（保留先出现的）
    seen: set[str] = set()

    def _dedupe_node(node: dict[str, Any], path: str) -> None:
        nid = str(node.get("id") or "node")
        if nid in seen:
            new_id = f"{path}-{nid}" if path else f"dup-{nid}"
            # 再撞则加序号
            base, i = new_id, 2
            while new_id in seen:
                new_id = f"{base}-{i}"
                i += 1
            notes.append(f"重命名重复 node id {nid!r} → {new_id!r}")
            node["id"] = new_id
            nid = new_id
        seen.add(nid)
        sub = node.get("sub_node")
        if isinstance(sub, dict):
            _dedupe_node(sub, nid)
        for child in node.get("sub_nodes") or []:
            if isinstance(child, dict):
                _dedupe_node(child, nid)

    for n in nodes:
        if isinstance(n, dict):
            _dedupe_node(n, "")

    node_ids = {str(n.get("id")) for n in _walk_node_dicts(nodes) if n.get("id")}
    # 先取再判断：写成 `module.get(...) if isinstance(module.get(...), list)`
    # 是两次独立调用，类型收窄不会传递到赋值结果上。
    raw_npcs = module.get("npcs")
    npcs: list[Any] = raw_npcs if isinstance(raw_npcs, list) else []
    npc_ids = {str(n.get("id")) for n in npcs if isinstance(n, dict) and n.get("id")}

    # 2) 边只保留存在的 node id（npc 目标丢弃）
    for n in _walk_node_dicts(nodes):
        for key in ("leads_to", "exits", "contains"):
            raw = n.get(key)
            if not isinstance(raw, list):
                continue
            kept = [str(t) for t in raw if str(t) in node_ids]
            dropped = [str(t) for t in raw if str(t) not in node_ids]
            if dropped:
                notes.append(f"node {n.get('id')!r} 丢弃悬空 {key}: {dropped}")
            n[key] = kept

    # 3) same_as 只保留存在的 npc
    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        raw = npc.get("same_as")
        if not isinstance(raw, list):
            continue
        kept = [str(t) for t in raw if str(t) in npc_ids]
        dropped = [str(t) for t in raw if str(t) not in npc_ids]
        if dropped:
            notes.append(f"npc {npc.get('id')!r} 丢弃悬空 same_as: {dropped}")
        npc["same_as"] = kept

    # 4) visibility_pairs 引用必须 ∈ node∪npc
    entity = node_ids | npc_ids
    pairs = module.get("visibility_pairs")
    if isinstance(pairs, list):
        cleaned: list[dict[str, Any]] = []
        for p in pairs:
            if not isinstance(p, dict):
                continue
            pref, sref = str(p.get("public_ref") or ""), str(p.get("secret_ref") or "")
            if pref in entity and sref in entity and pref != sref:
                cleaned.append(p)
            else:
                notes.append(f"丢弃非法 visibility_pair {p.get('id')!r} ({pref}↔{sref})")
        module["visibility_pairs"] = cleaned

    return notes


def compose_module(
    top: dict[str, Any],
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
    endings: list[dict[str, Any]],
    agenda: list[dict[str, Any]],
    visibility_pairs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mod: dict[str, Any] = {
        "meta": top["meta"],
        "kp_truth": top["kp_truth"],
        "player_intro": top["player_intro"],
        "nodes": nodes,
        "npcs": npcs,
        "endings": endings,
        "agenda": agenda,
        "kp_guidance": top.get("kp_guidance") or {},
        "visibility_pairs": visibility_pairs or [],
    }
    if top.get("opening"):
        mod["opening"] = top["opening"]
    return mod


def _ensure_node_minimums(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保证每个 node 有 title/kp_text，缺则填占位，让 schema 能过。"""
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        row = dict(n)
        row.setdefault("id", "unknown-node")
        row.setdefault("title", row["id"])
        if not row.get("kp_text"):
            row["kp_text"] = "（组装层未生成 kp_text）"
        row.setdefault("checks", [])
        row.setdefault("branches", [])
        row.setdefault("leads_to", [])
        row.setdefault("exits", [])
        row.setdefault("contains", [])
        row.setdefault("sub_nodes", [])
        if isinstance(row.get("sub_node"), dict):
            fixed_subs = _ensure_node_minimums([row["sub_node"]])
            row["sub_node"] = fixed_subs[0] if fixed_subs else row["sub_node"]
        if isinstance(row.get("sub_nodes"), list):
            row["sub_nodes"] = _ensure_node_minimums(
                [c for c in row["sub_nodes"] if isinstance(c, dict)]
            )
        out.append(row)
    return out


def _ensure_npc_minimums(npcs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in npcs:
        if not isinstance(n, dict):
            continue
        row = dict(n)
        row.setdefault("id", "unknown-npc")
        if not row.get("name"):
            row["name"] = row["id"]
        row.setdefault("forms", [])
        row.setdefault("same_as", [])
        out.append(row)
    return out


def _heuristic_visibility_pairs(
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 same_as 与 id 命名启发生成密级配对（无 LLM）。"""
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    npc_ids = {str(n.get("id")) for n in npcs if isinstance(n, dict) and n.get("id")}

    def _add(public_ref: str, secret_ref: str, note: str) -> None:
        if public_ref == secret_ref:
            return
        key = (public_ref, secret_ref)
        rev = (secret_ref, public_ref)
        if key in seen or rev in seen:
            return
        seen.add(key)
        pairs.append(
            {
                "id": f"pair-{public_ref}-{secret_ref}"[:64],
                "public_ref": public_ref,
                "secret_ref": secret_ref,
                "note": note,
            }
        )

    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        nid = str(npc.get("id") or "")
        for other in npc.get("same_as") or []:
            other_s = str(other)
            if other_s in npc_ids:
                # 启发式：id 含 secret/public 时定向，否则双向记一条 public→other
                if "secret" in nid.lower() or "真相" in nid:
                    _add(other_s, nid, "same_as 启发")
                elif "public" in nid.lower() or "公开" in nid:
                    _add(nid, other_s, "same_as 启发")
                else:
                    _add(nid, other_s, "same_as 启发")

    # id 对：foo-public / foo-secret 或 foo-public-persona / foo-secret
    by_stem: dict[str, dict[str, str]] = {}
    for nid in npc_ids:
        low = nid.lower()
        stem = (
            low.replace("-public-persona", "")
            .replace("-public", "")
            .replace("-secret", "")
            .replace("_public", "")
            .replace("_secret", "")
        )
        slot = by_stem.setdefault(stem, {})
        if "public" in low or "公开" in nid:
            slot["public"] = nid
        if "secret" in low or "真相" in nid or "true" in low:
            slot["secret"] = nid
    for stem, slots in by_stem.items():
        if "public" in slots and "secret" in slots:
            _add(slots["public"], slots["secret"], f"id 命名启发:{stem}")

    # node 侧：*public* / *secret* 成对
    node_ids = [str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")]
    node_set = set(node_ids)
    for nid in node_ids:
        low = nid.lower()
        if "public" in low:
            cand = nid.lower().replace("public", "secret")
            # 保持原大小写尽量：简单扫描
            for other in node_set:
                if other.lower() == cand and other != nid:
                    _add(nid, other, "node id 命名启发")
    return pairs


def stage3b_visibility_pairs(
    client: TapedSyncClient | None,
    *,
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    stats: CallStats | None,
    schema_doc: str,
) -> list[dict[str, Any]]:
    """3b：密级配对。先启发式，可选 LLM 补洞（无 client 则只启发式）。"""
    pairs = _heuristic_visibility_pairs(nodes, npcs)
    if client is None or stats is None:
        return pairs

    # 关系里像「公开/假象/真相」的边，喂 LLM 一次补全
    hints: list[str] = []
    for r in rels:
        text = str(r.get("relation") or "")
        if any(k in text for k in ("公开", "假象", "真相", "配对", "表面", "实际")):
            hints.append(f"{r.get('item_a')} ↔ {r.get('item_b')}: {text[:120]}")
    if not hints and pairs:
        return pairs

    system = (
        "你是模组预处理流水线的「密级配对」阶段。根据已成形的 node/npc id 列表与"
        "关系提示，产出 visibility_pairs[]。"
        "public_ref 与 secret_ref 必须是已有 node 或 npc 的 id。"
        "不要发明 id。若无从配对，输出空数组。\n"
        '只输出 JSON：{"visibility_pairs": [...]}\n\n' + schema_doc
    )
    node_ids = [str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")]
    npc_ids = [str(n.get("id")) for n in npcs if isinstance(n, dict) and n.get("id")]
    user = (
        f"【node ids】{', '.join(node_ids)}\n"
        f"【npc ids】{', '.join(npc_ids)}\n"
        f"【已有启发式 pairs】{json.dumps(pairs, ensure_ascii=False)}\n"
        f"【关系提示】\n" + ("\n".join(hints[:40]) if hints else "（无）") + "\n"
        "请合并/补全 visibility_pairs（可保留启发式结果）。"
    )
    try:
        data = _chat_json(
            client,
            system=system,
            user=user,
            temperature=TEMPERATURE,
            stats=stats,
            label="stage3b.visibility_pairs",
        )
        arr = data.get("visibility_pairs")
        if isinstance(arr, list) and arr:
            cleaned: list[dict[str, Any]] = []
            entity = set(node_ids) | set(npc_ids)
            for p in arr:
                if not isinstance(p, dict):
                    continue
                pref = str(p.get("public_ref") or "")
                sref = str(p.get("secret_ref") or "")
                if pref in entity and sref in entity and pref != sref:
                    cleaned.append(
                        {
                            "id": str(p.get("id") or f"pair-{pref}-{sref}")[:64],
                            "public_ref": pref,
                            "secret_ref": sref,
                            "note": p.get("note"),
                        }
                    )
            if cleaned:
                return cleaned
    except Exception as exc:  # noqa: BLE001 — 配对失败不阻断管线
        print(f"  stage3b warn: {exc}", flush=True)
    return pairs


def _ensure_ending_minimums(endings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in endings:
        if not isinstance(e, dict):
            continue
        row = dict(e)
        row.setdefault("id", "unknown-ending")
        row.setdefault("title", row["id"])
        if not row.get("text"):
            row["text"] = "（组装层未生成结局文本）"
        out.append(row)
    return out


def _ensure_agenda_minimums(agenda: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in agenda:
        if not isinstance(a, dict):
            continue
        row = dict(a)
        row.setdefault("id", "unknown-agenda")
        if not row.get("trigger"):
            row["trigger"] = "（触发条件待补）"
        if not row.get("kp_text"):
            row["kp_text"] = "（议程内容待补）"
        row.setdefault("effects", [])
        row.setdefault("once", True)
        out.append(row)
    return out


def _fidelity_entity_ids(report: ValidationReport) -> list[str]:
    """从 `[trace]`/`[numeric]` 错误里取出实体 id。

    🔴 这里解析的是**我们自己产生**的错误文本，格式由本仓库控制（`kind 'id' …`），
    不是在猜第三方字符串。仍然做成一个函数而不是散在调用处，是因为一旦那两条
    消息改了措辞，只有这里要跟着改。
    """
    out: list[str] = []
    for err in list(report.trace_errors) + list(report.numeric_errors):
        m = re.search(r"'([^']+)'", err)
        if m:
            out.append(m.group(1))
    return list(dict.fromkeys(out))


def _source_excerpt(anchors: dict[str, str], entity_ids: list[str], limit: int = 600) -> str:
    """出问题实体各自的源行，拼成给自修器看的原文摘录。

    🔴 **只给出问题的那几个实体**，不给全文：全文会把 prompt 撑爆、也更贵，
    而自修要的只是「这一段原文到底怎么写的」。
    """
    parts: list[str] = []
    for eid in entity_ids:
        text = anchors.get(eid)
        if text:
            parts.append(f"- {eid}：{text[:limit]}")
    return "\n".join(parts)


#: 这几类错误挂在**某一个实体**上，可以只重吐那一个实体（输出有界）。
#: 其余（leak / structure / facts / schema）跨越整份产物，只能整份修。
_ENTITY_SCOPED = ("[skill]", "[trace]", "[numeric]")


def entity_scoped_errors(report: ValidationReport) -> dict[str, list[str]]:
    """把实体级错误按实体 id 归拢。"""
    grouped: dict[str, list[str]] = {}
    for err in report.all_errors():
        if not err.startswith(_ENTITY_SCOPED):
            continue
        m = re.search(r"'([^']+)'", err)
        if m:
            grouped.setdefault(m.group(1), []).append(err)
    return grouped


def find_entity(module: dict[str, Any], entity_id: str) -> tuple[list[Any], int] | None:
    """在模组里定位实体，返回 `(所在列表, 下标)` 以便原地替换。含 `sub_nodes` 递归。"""

    def walk(nodes: list[Any]) -> tuple[list[Any], int] | None:
        for i, nd in enumerate(nodes):
            if not isinstance(nd, dict):
                continue
            if str(nd.get("id")) == entity_id:
                return nodes, i
            hit = walk(nd.get("sub_nodes") or [])
            if hit:
                return hit
        return None

    found = walk(module.get("nodes") or [])
    if found:
        return found
    for key in ("npcs", "endings", "agenda"):
        arr = module.get(key) or []
        for i, obj in enumerate(arr):
            if isinstance(obj, dict) and str(obj.get("id")) == entity_id:
                return arr, i
    return None


#: `check_leak` 报出来的那句话的形状。解析它是为了知道**改哪个字段、避哪些词**。
_LEAK_ERROR = re.compile(r"真相关键词 '(?P<kw>[^']+)' 出现在玩家可见字段 (?P<field>\S+)")

#: 泄密只可能发生在这两个字段上（`check_leak` 只扫这两个）。
#: 值是取/放这段文本的函数对——写死路径比传字符串路径可读，也不会拼错。
_LEAK_FIELDS = ("player_intro", "opening.script")


def leaky_fields(errors: list[str]) -> dict[str, list[str]]:
    """把 `[leak]` 错误按字段归拢成 `字段 → 要避开的词`。"""
    out: dict[str, list[str]] = {}
    for err in errors:
        m = _LEAK_ERROR.search(err)
        if not m or m.group("field") not in _LEAK_FIELDS:
            continue
        kws = out.setdefault(m.group("field"), [])
        if m.group("kw") not in kws:
            kws.append(m.group("kw"))
    return out


def read_leaky_field(module: dict[str, Any], field: str) -> str:
    if field == "player_intro":
        return str(module.get("player_intro") or "")
    opening = module.get("opening")
    return str(opening.get("script") or "") if isinstance(opening, dict) else ""


def write_leaky_field(module: dict[str, Any], field: str, text: str) -> None:
    if field == "player_intro":
        module["player_intro"] = text
        return
    opening = module.get("opening")
    if isinstance(opening, dict):
        opening["script"] = text


#: `check_encounter_reachability` 报出来那句话的形状。解析它是为了知道**接哪个节点**。
_REACH_ERROR = re.compile(r"遭遇节点 '(?P<node>[^']+)' 没有任何入边")


def dangling_encounter_ids(errors: list[str]) -> list[str]:
    """把 `[reach]` 错误解析成悬空的遭遇节点 id 列表（保序去重）。"""
    out: list[str] = []
    for err in errors:
        m = _REACH_ERROR.search(err)
        if m and m.group("node") not in out:
            out.append(m.group("node"))
    return out


def attach_encounter(module: dict[str, Any], parent_id: str, node_id: str) -> bool:
    """把 `node_id` 接到 `parent_id` 的 `leads_to` 上。纯代码，不问模型。

    父节点必须已存在且不能是它自己（自环接不出可达性）。返回是否真的写进去了。
    """
    if parent_id == node_id:
        return False
    for node in _walk_node_dicts(module.get("nodes") or []):
        if node.get("id") != parent_id:
            continue
        leads = node.get("leads_to")
        if not isinstance(leads, list):
            leads = []
            node["leads_to"] = leads
        if node_id not in leads:
            leads.append(node_id)
        return True
    return False


REACH_REPAIR_SYSTEM = """\
你在修一份已经组装好的 COC 模组：有一幕遭遇没有任何节点引出它，玩家永远走不到。

请从【现有节点】里挑**一个**最该引出这一幕的节点——通常是玩家在剧情上紧接着
会走到那一幕的前一处（比如上一幕遭遇，或者这一幕发生的那个地点）。

只输出 JSON：{"from": "<节点 id>"}
- id 必须**逐字**来自【现有节点】清单，不要发明新 id。
- 不要选那一幕自己。
- 挑不出合理的就输出 {"from": null}——**不要硬凑一个**。
"""


def repair_dangling_encounter(
    client: TapedSyncClient,
    *,
    module: dict[str, Any],
    node_id: str,
    stats: CallStats,
    label: str,
) -> str | None:
    """问一句「哪个节点该引出这一幕」，**只回一个 id**。

    ## 🔴 为什么单独给它一条路（2026-08-10 真机拒绝后补）

    `reach` 是跨实体错误（缺的边在**别的**节点上），所以它一开始被丢给了整份
    重吐——而那条路在真实体量的模组上是结构性失败。真机实测：两轮自修
    `repair#1`/`repair#2` 各三次尝试**全部断在同一位置**（`Unterminated string
    ... line 629`），耗时 324 秒，最后仍是拒绝。跟 `leak` 当初一模一样的病。

    但这条错误的作用域其实是**所有类别里最小的**：缺的就是**一个 id**。
    所以照 `repair_leaky_text` 的形状给它一条窄路——输出一行，
    **结构上不可能被截断**，然后由 `attach_encounter` 用纯代码写进图里。

    ## 边界

    - 模型挑不出来（`from` 为 null 或不在清单里）就**返回 None，照旧拒绝**。
      不做「随便接到第一个节点上」之类的兜底——那会把一幕戏接到毫无关系的
      地方，比拒绝更糟（禁止静默兜底）。
    - 修法**只加一条边**，不动那一幕本身的任何文字。
    """
    nodes = _walk_node_dicts(module.get("nodes") or [])

    def _line(n: dict[str, Any]) -> str:
        mark = "·遭遇" if str(n.get("kind") or "").strip().lower() == ENCOUNTER_KIND else ""
        return f"- {n.get('id')}（{n.get('title') or '无标题'}{mark}）"

    catalog = "\n".join(_line(n) for n in nodes if n.get("id") and n.get("id") != node_id)
    target = next((n for n in nodes if n.get("id") == node_id), {})
    user = (
        f"【走不到的那一幕】\n{node_id}（{target.get('title') or ''}）\n"
        f"{str(target.get('kp_text') or '')[:400]}\n\n"
        f"【现有节点】\n{catalog}\n\n"
        "哪个节点应该引出这一幕？"
    )
    data = _chat_json(
        client,
        system=REACH_REPAIR_SYSTEM,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label=label,
    )
    picked = data.get("from")
    if not isinstance(picked, str) or not picked.strip():
        return None
    picked = picked.strip()
    known = {str(n.get("id")) for n in nodes if n.get("id")}
    # 🔴 模型写的 id 一律过白名单——同「不要用自由文本当标识符」。
    return picked if picked in known and picked != node_id else None


def repair_leaky_text(
    client: TapedSyncClient,
    *,
    field: str,
    text: str,
    keywords: list[str],
    stats: CallStats,
    label: str,
) -> str:
    """把一段**玩家开场就会读到**的文字里的真相摘出去，只重吐这一段。

    ## 🔴 为什么单独给它一条路

    `leak` 是跨实体错误，此前唯一的修法是**整份重吐**——而那条路在真实体量的
    模组上是结构性失败（林中屋产物 25407 字符，响应截在 21916，三次全断在同一
    位置）。于是 leak 实际上**修不掉**，只能变成一次拒绝。

    但泄密的作用域其实很小：`check_leak` 只扫 `player_intro` 和 `opening.script`
    两个字段，各是一段话。重吐一段话输出长度有界，跟分片修实体是同一个道理。

    ## 边界

    改写完仍然泄密就是**真失败**，照旧拒绝——不做「把这段删掉」之类的兜底。
    删掉玩家开场白是把模组悄悄弄残，比拒绝更糟（禁止静默兜底）。
    """
    system = (
        "你在改写一段**玩家在开场就会读到**的文字。\n\n"
        "下面列出的词属于守秘人的绝密真相，玩家此刻不该知道。要求：\n"
        "- 这些词**一个都不能出现**；也不要换成同义词或用暗示的方式说出同一件事\n"
        "- 保持原来的长度、语气和信息密度，只把涉及那些内容的地方改写成"
        "**玩家此刻真的知道的样子**（通常是更含糊、更外部视角的说法）\n"
        "- 不要新增原文没有的设定\n"
        "- 只输出改写后的正文，不要解释、不要加引号、不要 markdown\n"
    )
    user = f"【不能出现的词】\n{'、'.join(keywords)}\n\n【要改写的字段】{field}\n\n【原文】\n{text}"
    # 这一段是散文不是 JSON，所以不走 `_chat_json`。
    tape_key = tape_key_for(stats, label)
    response = client.chat.completions.create(
        tape_kind="module_assemble",
        tape_key=tape_key,
        model=DEEPSEEK_MODEL,
        temperature=TEMPERATURE,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    usage = response.usage
    with stats.lock:
        stats.calls += 1
        if usage is not None:
            stats.prompt_tokens += usage.prompt_tokens or 0
            stats.completion_tokens += usage.completion_tokens or 0
            stats.total_tokens += usage.total_tokens or 0
    out = (response.choices[0].message.content or "").strip()
    if not out:
        raise RuntimeError(f"{label}：改写返回空文本")
    return out


def repair_entity(
    client: TapedSyncClient,
    *,
    entity: dict[str, Any],
    errors: list[str],
    source_excerpt: str,
    stats: CallStats,
    label: str,
) -> dict[str, Any]:
    """只重吐**一个实体**。

    🔴 这是为了让**输出长度有界**。整份重吐在真实体量的模组上结构性地吐不完：
    实测林中屋产物 25407 字符，自修响应被截在 21916 —— 三次尝试全断在同一位置。
    而自修是拒绝率的最后一道缓冲，它坏了拒绝率就等于首次校验的失败率。
    """
    system = (
        "你在修一份 TRPG 模组结构化数据里的**一个实体**。"
        "只输出修正后的那个实体的 JSON 对象，保持它的 id 不变，不要输出别的东西。\n\n"
        "修正要求：\n"
        # 🔴 自修器也得拿到白名单。它逐条列着"该修什么"，而「未归一到技能 id」
        # 这类错误说的正是"你写的名字不在表里"——不给它表，它只能再猜一次。
        "- checks[].skill_ids 逐字取自下面的白名单；skill 填表里对应的中文名。"
        "表里找不到对应的，把整条检定删掉，不要造名字。\n"
        "- 数值（骰型/百分比）必须**逐字**来自【原文】。最常见的坏法是**把两个数粘成"
        "一个**——「爪击70 1D6+1」是「技能值 70」与「伤害 1D6+1」两个数，写成 701d6+1 "
        "就是错的。原文里没有的数字**一律删掉，不要另编一个**。\n"
        "- 文字要能对上【原文】；对不上就照原文重写，**宁可写短，也不要编**。\n"
        "- 不要新增字段，不要改 id。\n"
    )
    if any(e.startswith("[skill]") or "技能 id" in e for e in errors):
        system += "\n" + render_skill_whitelist()
    user = (
        "【这个实体的问题】\n" + "\n".join(f"- {e}" for e in errors) + "\n\n"
        f"【原文】（照它改，不要凭印象）\n{source_excerpt or '（这个实体没有可用的原文锚点）'}\n\n"
        f"【当前实体 JSON】\n{json.dumps(entity, ensure_ascii=False, indent=2)}"
    )
    data = _chat_json(
        client, system=system, user=user, temperature=TEMPERATURE, stats=stats, label=label
    )
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("实体自修返回的不是一个带 id 的对象")
    return data


def repair_module(
    client: TapedSyncClient,
    *,
    module: dict[str, Any],
    report: ValidationReport,
    stats: CallStats,
    schema_doc: str,
    attempt: int,
    source_excerpt: str = "",
) -> dict[str, Any]:
    """把错误清单 + 当前 JSON（+ 相关原文）喂回 LLM，要求整份修正。"""
    system = (
        "你是模组预处理流水线的「自修」阶段。收到一份 ScenarioModule JSON 与"
        "机械校验错误清单。请输出修正后的**完整** ScenarioModule JSON（不要只给 diff）。"
        "必须消除清单中的每一项错误。\n\n"
        "特别注意：\n"
        "- 技能名必须是 COC7 中文名（侦察 不是 侦查 也可，二者都接受；"
        "斗殴 应写作 格斗：斗殴；手枪 应写作 射击：手枪）。\n"
        "- leads_to / exits / contains 只能引用已存在的 node id；悬空引用请删除或改成存在的 id。\n"
        "- visibility_pairs 的 public_ref/secret_ref 必须是已有 node 或 npc id；悬空则删。\n"
        "- npc.same_as 只能引用已有 npc id。\n"
        "- 不泄密：从 key_facts 抽出的关键词不得出现在 player_intro 与 opening.script；"
        "可改写玩家可见字段，或把过细的关键词从 key_facts 改成更抽象的表述。\n"
        "- node.kp_text 可以含真相，不要为了「不泄密」去清空 kp_text。\n"
        # 🔴 这条原来是「endings/agenda 为空就去补一条」——那正是伪造结局的另一
        # 条路（`exec/29`：林中屋的尾声就是这么变成 endings[0] 的）。门已改成
        # 「不许吞进 kp_truth」，这里必须跟着改，否则自修器还在按旧门修。
        "- 若 structure 报「信号片段被吞进 kp_truth」：把那段材料从 kp_truth 挪到它真正的"
        "归宿（玩家能走到的收束点→endings[]；尾声/奖励/续跑建议→kp_guidance；"
        "时间压力→agenda[]）。**不要凭空造一条结局来交差**；endings[] 允许为空。\n"
        # 🔴 下面两类是 exec/29 新增的忠实度门。加了门却不告诉自修器怎么修，
        # 它就只会在别处瞎改 —— 实测林中屋那 3 条 numeric 自修 1 轮没动过。
        "- numeric（数值对不上原文）：产物里的骰型/百分比必须**逐字**来自原文。"
        "最常见的坏法是**把两个数粘成一个**——NPC 属性块里「爪击70 1D6+1」是"
        "「技能值 70」与「伤害 1D6+1」两个数，写成 701d6+1 就是错的。"
        "照【相关原文】改回去；原文里没有的数字**一律删掉，不要另编一个**。\n"
        "- trace（追不回原文）：每个实体的文字都要能对上它的出处。"
        "对不上就照【相关原文】重写那一条；**宁可写短，也不要编**。\n"
        # 🔴 `reach` **不走这条路**（2026-08-10）：它一度被丢到整份重吐这里，
        # 真机实测两轮六次尝试全断在同一位置、324 秒后仍是拒绝。它的作用域其实
        # 只有一个 id，已改走 `repair_dangling_encounter` 那条窄路。这里不要再
        # 给它写指示——写了等于把它请回这条注定失败的路上。\n
        "- 只输出一个 JSON 对象，即完整模组。\n\n" + schema_doc
    )
    excerpt_block = (
        f"\n\n【相关原文】（照它改，不要凭印象）\n{source_excerpt}" if source_excerpt else ""
    )
    user = (
        f"【校验错误】\n{report.summary_text()}{excerpt_block}\n\n"
        f"【当前模组 JSON】\n{json.dumps(module, ensure_ascii=False, indent=2)}"
    )
    data = _chat_json(
        client,
        system=system,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label=f"repair#{attempt}",
    )
    # 若模型包了一层
    if "meta" not in data and isinstance(data.get("module"), dict):
        data = data["module"]
    return data


def _print_stage1_summary(stage1: dict[str, Any]) -> None:
    print(
        f"  entities: {len(stage1['entities'])}, "
        f"assignments: {len(stage1['assignment_map'])}, "
        f"orphans: {len(stage1['orphans'])}",
        flush=True,
    )
    kind_counts: dict[str, int] = {}
    for info in stage1["assignment_map"].values():
        k = str(info.get("dest_kind") or "?")
        kind_counts[k] = kind_counts.get(k, 0) + 1
    print(f"  dest_kind 分布: {kind_counts}", flush=True)
    for ent in stage1["entities"]:
        print(
            f"    - {ent['kind']}:{ent['id']} ← {len(ent.get('item_ids') or [])} items",
            flush=True,
        )


def _run_stage2_and_3(
    client: TapedSyncClient,
    *,
    stage1: dict[str, Any],
    items: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    rels: list[dict[str, Any]],
    schema_doc: str,
    title_hint: str,
    stats: CallStats,
    repair_notes: str = "",
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """阶段2+3：返回 (top, nodes, npcs, endings, agenda, stage_meta)。"""
    known_node_ids = [e["id"] for e in stage1["entities"] if e.get("kind") == "node"]

    print("\n=== 阶段 2 · 实体成形 ===", flush=True)
    t2 = time.perf_counter()
    nodes = stage2_form_kind(
        client,
        kind="node",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    npcs = stage2_form_kind(
        client,
        kind="npc",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    endings = stage2_form_kind(
        client,
        kind="ending",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    agenda = stage2_form_kind(
        client,
        kind="agenda",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    nodes = _ensure_node_minimums(nodes)
    npcs = _ensure_npc_minimums(npcs)
    endings = _ensure_ending_minimums(endings)
    agenda = _ensure_agenda_minimums(agenda)
    print(
        f"  formed: nodes={len(nodes)} npcs={len(npcs)} "
        f"endings={len(endings)} agenda={len(agenda)}",
        flush=True,
    )
    stage2_meta = {
        "node_ids": [n.get("id") for n in nodes],
        "npc_ids": [n.get("id") for n in npcs],
        "ending_ids": [e.get("id") for e in endings],
        "agenda_ids": [a.get("id") for a in agenda],
        "elapsed_s": round(time.perf_counter() - t2, 2),
    }

    print("\n=== 阶段 3 · 顶层字段 ===", flush=True)
    t3 = time.perf_counter()
    top = stage3_toplevel(
        client,
        items=items,
        items_by_id=items_by_id,
        lines=lines,
        stage1=stage1,
        schema_doc=schema_doc,
        stats=stats,
        module_title_hint=title_hint,
        repair_notes=repair_notes,
    )
    stage3_meta = {
        "meta_id": top["meta"].get("id"),
        "key_facts_count": len(top["kp_truth"].get("key_facts") or []),
        "kp_guidance_keys": list((top.get("kp_guidance") or {}).keys()),
        "player_intro_len": len(top.get("player_intro") or ""),
        "elapsed_s": round(time.perf_counter() - t3, 2),
    }
    return top, nodes, npcs, endings, agenda, {"stage2": stage2_meta, "stage3": stage3_meta}


def _assignment_thin_counts(assignment_map: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in assignment_map.values():
        k = str(info.get("dest_kind") or "")
        if k in ("player_intro", "opening", "meta"):
            counts[k] = counts.get(k, 0) + 1
    return counts


#: 一串 sha256 长这样：64 个十六进制字符。上传件就是按它命名的。
_HASHY = re.compile(r"^[0-9a-f]{32,}$")


def resolve_title_hint(explicit: str | None, extract_path: Path) -> str:
    """模组标题的外部线索——给顶层 prompt 用。

    🔴 **web 导入那条路上文件名是一串 sha256。** 上传件按内容哈希存盘
    （`uploads/f43acfd8….pdf`），于是从 `extract_path` 词干推出来的「模组标题
    提示」等于**没有提示**：模型只能从正文里自己挑一个，74 节点那份就是这么
    把一个小标题当成了模组名（`meta.id` 反而对——抽 id 那一步看的不是它）。

    手工跑法（`模组资料/林中屋.重组.裸抽取.json`）一直是对的，所以这条只在
    真正上传时才犯 —— 又一次「换一条路径验出来的『好了』」。

    调用方给了真实文件名就用它；没给才退回词干，而**词干看着像哈希就说出来**，
    不假装那是一个标题（「禁止静默兜底」）。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    stem = extract_path.name.split(".")[0]
    if _HASHY.match(stem):
        return "（未知，文件名没有标题信息，请从正文判断）"
    return stem


def run_pipeline(
    *,
    extract_path: Path,
    pass1_path: Path,
    pass2_path: Path,
    source_txt: Path,
    example_path: Path | None,
    out_structured: Path,
    out_intermediate: Path,
    out_report: Path,
    title_hint: str | None = None,
) -> int:
    print(f"extract: {extract_path}", flush=True)
    print(f"source:  {source_txt}", flush=True)

    extract = json.loads(extract_path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = extract["items"]
    source_item_ids = {str(it["id"]) for it in items if it.get("id")}
    items_by_id = {str(it["id"]): it for it in items if it.get("id")}
    print(f"items: {len(items)}", flush=True)

    p1 = json.loads(pass1_path.read_text(encoding="utf-8"))
    p2 = json.loads(pass2_path.read_text(encoding="utf-8"))
    rels = merge_relations(p1.get("relations") or [], p2.get("relations") or [])
    print(f"relations merged: {len(rels)}", flush=True)

    # 🔴 裸抽取里的 line_start/line_end 是**相对它当时读的那个文件**的。
    # `--source-txt` 传成另一个（`.txt` 而不是 `.重组.txt`）会让行号整体错位，
    # 而症状是溯源校验凭空报一堆"疑似编造"——查了半天才发现是拿错了原文。
    # 所以这里对齐一次，不一致就当场退出，不给它静默走下去的机会。
    recorded_source = str(extract.get("source") or "")
    if recorded_source and Path(recorded_source).resolve() != source_txt.resolve():
        raise SystemExit(
            f"--source-txt 与裸抽取记录的原文不是同一个文件，行号会错位：\n"
            f"  裸抽取用的：{recorded_source}\n"
            f"  本次传入的：{source_txt.resolve()}"
        )
    lines = read_numbered_lines(source_txt)
    schema_doc = load_example_skeleton(example_path)
    title_hint = resolve_title_hint(title_hint, extract_path)

    api_key = load_api_key()
    client = build_sync_llm_client(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=180.0)
    stats = CallStats()
    t_all = time.perf_counter()

    # 🔴 受众翻译：自由文本 → 枚举。必须在归组**之前**——归组模型要靠它才知道
    # 一个叫 `introduction` 的片段其实是 KP 内容（真机那次它每轮都归进 opening）。
    audience_kinds = translate_audiences(client, items, stats)
    apply_audience_kinds(items, audience_kinds)
    print(
        f"audience: {len(audience_kinds)} 种写法 → {sorted(set(audience_kinds.values()))}",
        flush=True,
    )

    intermediate: dict[str, Any] = {
        "source_extract": str(extract_path),
        "source_txt": str(source_txt),
        "item_count": len(items),
        "relation_count": len(rels),
    }

    # ── 阶段 1 ──
    print("\n=== 阶段 1 · 实体归组 ===", flush=True)
    t1 = time.perf_counter()
    stage1 = stage1_group(client, items, rels, stats)
    _print_stage1_summary(stage1)
    intermediate["stage1"] = {
        "entities": stage1["entities"],
        "assignments": stage1["assignments"],
        "orphans": stage1["orphans"],
        "thin_slot_counts": _assignment_thin_counts(stage1["assignment_map"]),
        "elapsed_s": round(time.perf_counter() - t1, 2),
    }

    # 若大量 unassigned，再给一次归组修正机会（不算自修次数）
    unassigned = [
        iid
        for iid, info in stage1["assignment_map"].items()
        if info.get("dest_kind") == "unassigned"
    ]
    if unassigned:
        print(
            f"  有 {len(unassigned)} 个 unassigned，再跑一次归组修正…",
            flush=True,
        )
        notes = "以下片段被标为 unassigned，请尽量归入合理实体：\n" + "\n".join(
            f"- {i}" for i in unassigned
        )
        stage1 = stage1_group(client, items, rels, stats, repair_notes=notes)
        _print_stage1_summary(stage1)
        intermediate["stage1"] = {
            "entities": stage1["entities"],
            "assignments": stage1["assignments"],
            "orphans": stage1["orphans"],
            "thin_slot_counts": _assignment_thin_counts(stage1["assignment_map"]),
            "elapsed_s": round(time.perf_counter() - t1, 2),
            "had_regroup": True,
        }

    top, nodes, npcs, endings, agenda, s23 = _run_stage2_and_3(
        client,
        stage1=stage1,
        items=items,
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        schema_doc=schema_doc,
        title_hint=title_hint,
        stats=stats,
    )
    intermediate["stage2"] = s23["stage2"]
    intermediate["stage3"] = s23["stage3"]

    print("\n=== 阶段 3b · 密级配对 ===", flush=True)
    visibility_pairs = stage3b_visibility_pairs(
        client,
        nodes=nodes,
        npcs=npcs,
        rels=rels,
        stats=stats,
        schema_doc=schema_doc,
    )
    intermediate["stage3b"] = {"pair_count": len(visibility_pairs)}
    print(f"  visibility_pairs: {len(visibility_pairs)}", flush=True)

    module = compose_module(top, nodes, npcs, endings, agenda, visibility_pairs=visibility_pairs)
    n_skill = normalize_and_prune_checks(module)
    if n_skill:
        print(f"  技能名机械归一：{n_skill} 处", flush=True)
    mech = mechanical_sanitize_module(module)
    if mech:
        print(f"  机械修补 {len(mech)} 项：", flush=True)
        for line in mech[:20]:
            print(f"    - {line}", flush=True)

    # ── 校验 + 自修 ──
    print("\n=== 校验闭环 ===", flush=True)
    assignment_map = stage1["assignment_map"]
    _moved = enforce_audience_slots(assignment_map, items_by_id)
    if _moved:
        print(f"  受众槽位强制：{len(_moved)} 条", flush=True)
        for line in _moved[:10]:
            print(f"    · {line}", flush=True)
    repair_count = 0
    report = validate_assembled(
        module,
        source_item_ids=source_item_ids,
        assignment_map=assignment_map,
        items=items,
        source_lines=lines,
    )
    print(report.summary_text(), flush=True)

    # 🔴 **保留最好的那一版，不是最后一版**（2026-08-20）。
    #
    # 真机实测：硬失败数走的是 3 → 2 → 6，而循环拒绝时交出去的是**最后一版**
    # ——手里明明有过一份只有 3 处问题的中间产物，被自己覆盖掉了。
    #
    # 自修每一轮都可能重跑归组，而归组是概率的：这一轮修好了 A，下一轮可能带出
    # 新的 B。没有"留底"的循环，跑得越多不一定越好。
    best_module = copy.deepcopy(module)
    best_report = report
    best_map = copy.deepcopy(assignment_map)

    stalled = 0
    while not report.ok and repair_count < MAX_REPAIR:
        # 🔴 **结算放在轮首，不是轮尾**：循环体里有好几处 `continue`，放轮尾的
        # 代码会被跳过——那正是「逐个列出的地方，加一项就漏一项」（reach 那条
        # 窄路就是这么被 continue 吃掉的）。放轮首，每条路径都必然经过它。
        if len(report.all_errors()) < len(best_report.all_errors()):
            best_module = copy.deepcopy(module)
            best_report = report
            best_map = copy.deepcopy(assignment_map)
            stalled = 0
        elif repair_count:
            # 这一轮没让它变好。连着两轮不见好就停——再跑只是烧钱，而且实测
            # 会**越修越糟**（真机那次 3 → 2 → 6）。
            stalled += 1
            if stalled >= 2:
                print("\n🔴 连续两轮没有改善，停止自修（继续跑只会更糟）", flush=True)
                break

        repair_count += 1
        print(f"\n--- 自修 #{repair_count} ---", flush=True)
        err_text = "\n".join(report.all_errors())

        if report.needs_stage1_repair():
            # 归组类失败：回灌阶段1，再重跑阶段2/3（06：错误喂回阶段1 重新归组）
            print("  → 归组类失败，回灌阶段1 重新归组…", flush=True)
            stage1 = stage1_group(client, items, rels, stats, repair_notes=err_text)
            _print_stage1_summary(stage1)
            intermediate["stage1"] = {
                "entities": stage1["entities"],
                "assignments": stage1["assignments"],
                "orphans": stage1["orphans"],
                "thin_slot_counts": _assignment_thin_counts(stage1["assignment_map"]),
                "elapsed_s": round(time.perf_counter() - t1, 2),
                f"repair_regroup_{repair_count}": True,
            }
            assignment_map = stage1["assignment_map"]
            _moved = enforce_audience_slots(assignment_map, items_by_id)
            if _moved:
                print(f"  受众槽位强制：{len(_moved)} 条", flush=True)
            top, nodes, npcs, endings, agenda, s23 = _run_stage2_and_3(
                client,
                stage1=stage1,
                items=items,
                items_by_id=items_by_id,
                lines=lines,
                rels=rels,
                schema_doc=schema_doc,
                title_hint=title_hint,
                stats=stats,
                repair_notes=err_text,
            )
            intermediate["stage2"] = s23["stage2"]
            intermediate["stage3"] = s23["stage3"]
            visibility_pairs = stage3b_visibility_pairs(
                client,
                nodes=nodes,
                npcs=npcs,
                rels=rels,
                stats=stats,
                schema_doc=schema_doc,
            )
            intermediate["stage3b"] = {"pair_count": len(visibility_pairs)}
            module = compose_module(
                top, nodes, npcs, endings, agenda, visibility_pairs=visibility_pairs
            )
            n_skill = normalize_and_prune_checks(module)
            if n_skill:
                print(f"  技能名机械归一：{n_skill} 处", flush=True)
            mech = mechanical_sanitize_module(module)
            if mech:
                print(f"  机械修补 {len(mech)} 项", flush=True)
            report = validate_assembled(
                module,
                source_item_ids=source_item_ids,
                assignment_map=assignment_map,
                items=items,
                source_lines=lines,
            )
            print(report.summary_text(), flush=True)
            # 归组修好后若只剩产物级问题，同轮再修一次 JSON（不另计 repair）
            #
            # 🔴 **只在 `report.ok` 时 continue**（2026-08-20 改）。原来的条件是
            # `report.ok or report.needs_stage1_repair()`，而后半句意味着"还是
            # 归组问题就跳到下一轮"——于是只要存在任何一个 secret_public /
            # orphan / thin_slot / structure 错误，**下面所有窄路一次都跑不到**。
            #
            # 真机实测：2026-08-10 专门为 reach 做的那条窄路（每个悬空节点单独
            # 一次小调用），在三轮自修里**一次都没被调用过**（日志里零个
            # `repair#` 标签），而 reach 错误在 3→2→6 之间乱跳——那正是每轮
            # 整份重吐带出来的新孤立节点。
            #
            # 判据：**兜底的触发条件要包含「主路失败」，不能只包含「主路没走」**
            # ——`if/continue` 会让主路失败吃掉兜底。
            if report.ok:
                continue

        # schema/ref/skill/leak：先机械归一+修补，仍失败再尝试整份 JSON 自修
        print("  → 产物级失败，技能归一 + 机械修补…", flush=True)
        normalize_and_prune_checks(module)
        mech = mechanical_sanitize_module(module)
        if mech:
            print(f"  机械修补 {len(mech)} 项", flush=True)
            for line in mech[:15]:
                print(f"    - {line}", flush=True)
        report = validate_assembled(
            module,
            source_item_ids=source_item_ids,
            assignment_map=assignment_map,
            items=items,
            source_lines=lines,
        )
        print(report.summary_text(), flush=True)
        if report.ok:
            continue

        # 仅非引用类错误才上 LLM 自修（引用类走机械修补）
        hard = [e for e in report.all_errors() if not e.startswith("[ref]")]
        if not hard:
            print("  → 仅剩 ref 类错误且机械修补后仍在，跳过 LLM 自修", flush=True)
            continue

        # 🔴 **先分片修**：实体级错误只重吐那一个实体，输出长度有界。
        # 整份重吐在真实体量的模组上结构性地吐不完（林中屋产物 25407 字符，
        # 自修响应截在 21916，三次尝试全断在同一位置）。
        anchors = build_entity_anchors(items, assignment_map, module, lines)
        grouped = entity_scoped_errors(report)
        patched = 0
        for eid, errs in grouped.items():
            spot = find_entity(module, eid)
            if spot is None:
                continue
            arr, idx = spot
            try:
                arr[idx] = repair_entity(
                    client,
                    entity=arr[idx],
                    errors=errs,
                    source_excerpt=_source_excerpt(anchors, [eid]),
                    stats=stats,
                    label=f"repair#{repair_count}.entity:{eid}",
                )
                patched += 1
            except RuntimeError as exc:
                print(f"    · 实体 {eid} 自修失败：{exc}", flush=True)
        if patched:
            print(f"  → 分片自修 {patched}/{len(grouped)} 个实体", flush=True)

        # 🔴 泄密单独走一条：它虽然是跨实体错误，作用域却只有两个短字段
        # （`check_leak` 只扫 player_intro / opening.script）。丢给整份重吐等于
        # 修不掉——那条路在真实体量上吐不完，leak 于是必然变成一次拒绝。
        leaks = leaky_fields(report.leak_errors)
        for leak_field, keywords in leaks.items():
            original = read_leaky_field(module, leak_field)
            if not original:
                continue
            try:
                write_leaky_field(
                    module,
                    leak_field,
                    repair_leaky_text(
                        client,
                        field=leak_field,
                        text=original,
                        keywords=keywords,
                        stats=stats,
                        label=f"repair#{repair_count}.leak:{leak_field}",
                    ),
                )
                print(
                    f"  → 重写玩家可见字段 {leak_field}（避开 {len(keywords)} 个真相词）",
                    flush=True,
                )
            except RuntimeError as exc:
                print(f"    · {leak_field} 改写失败：{exc}", flush=True)

        # 🔴 遭遇悬空同样单独走一条：它虽然是跨实体错误（缺的边在**别的**节点上），
        # 作用域却是所有类别里最小的——缺的就是一个 id。丢给整份重吐等于修不掉
        # （2026-08-10 真机实测：两轮自修六次尝试全断在同一位置，324 秒后仍是拒绝）。
        attached = 0
        for dangling in dangling_encounter_ids(report.reach_errors):
            try:
                parent = repair_dangling_encounter(
                    client,
                    module=module,
                    node_id=dangling,
                    stats=stats,
                    label=f"repair#{repair_count}.reach:{dangling}",
                )
            except RuntimeError as exc:
                print(f"    · 遭遇 {dangling} 接线失败：{exc}", flush=True)
                continue
            if parent is None:
                print(f"    · 遭遇 {dangling} 没挑出该由谁引出（不硬凑）", flush=True)
                continue
            if attach_encounter(module, parent, dangling):
                attached += 1
                print(f"  → 把遭遇 {dangling} 接到 {parent}.leads_to", flush=True)

        # 剩下的是跨实体错误（structure / facts / schema），只能整份修
        rest = [
            e
            for e in hard
            if not e.startswith(_ENTITY_SCOPED)
            and not e.startswith("[leak]")
            and not e.startswith("[reach]")
        ]
        if rest:
            print(f"  → 另有 {len(rest)} 条跨实体错误，尝试整份 JSON 自修…", flush=True)
            try:
                module = repair_module(
                    client,
                    module=module,
                    report=report,
                    stats=stats,
                    schema_doc=schema_doc,
                    attempt=repair_count,
                    source_excerpt=_source_excerpt(anchors, _fidelity_entity_ids(report)),
                )
            except RuntimeError as exc:
                # 🔴 不 break：分片修可能已经改好了一部分，让它走完下面的重新校验，
                # 拿到真实的剩余错误数，而不是把这一轮的成果一起丢掉。
                print(f"  → 整份自修失败（保留分片修的结果）：{exc}", flush=True)
        elif not patched and not leaks and not attached:
            # 🔴 `leaks` / `attached` 也要算进来：只修了泄密字段或只接了一条边时，
            # 这里会误判成"什么都没修"而提前 break，那一轮的成果被丢掉、
            # 还得多花一次自修额度。**每加一条窄修法，这个条件都要跟着加一项。**
            print("  → 没有可修的东西，停止自修", flush=True)
            break
        module["nodes"] = _ensure_node_minimums(module.get("nodes") or [])
        module["npcs"] = _ensure_npc_minimums(module.get("npcs") or [])
        module["endings"] = _ensure_ending_minimums(module.get("endings") or [])
        module["agenda"] = _ensure_agenda_minimums(module.get("agenda") or [])
        module.setdefault("visibility_pairs", [])
        normalize_and_prune_checks(module)
        mechanical_sanitize_module(module)

        report = validate_assembled(
            module,
            source_item_ids=source_item_ids,
            assignment_map=assignment_map,
            items=items,
            source_lines=lines,
        )
        print(report.summary_text(), flush=True)

    # ── 事实寻址（exec/14 P1.3）：facts / reveals / knows ──
    #
    # 🔴 导入进来的模组此前**根本不产 facts**（那一步只活在 `migrate_facts.py`
    # 的 CLI 里，内置模组是人工跑一遍的）。症状不是报错而是**恒空**：线索账本
    # 一条不显示、`check.reveals` 全空、"还有多少没揭开"永远是 0。
    #
    # 放在校验与自修**之后**、写文件之前，两个理由：
    # ① facts 由纯机械代码从 `on_success` / `kp_notes` / `key_facts` 重建，
    #    闭合性由构造保证——门是用来抓模型的，不必去校一份代码自己生成的东西；
    # ② 它会把产物撑大一两成，而「整份重吐」在真实体量上是结构性失败，
    #    不该让自修的输入白白变长。
    module, fact_stats = apply_fact_addressing(module)
    print(
        f"\n事实寻址：facts {fact_stats['facts_total']} 条"
        f"（检定产出 {fact_stats['on_success']} 处引用、合并 {fact_stats['deduped']} 处；"
        f"NPC 知情 {fact_stats['npc_knowledge']}；真相层 {fact_stats['key_facts']}）",
        flush=True,
    )

    elapsed_all = time.perf_counter() - t_all
    cost = stats.estimate_cost_cny()

    # 计数
    check_count = sum(len(n.get("checks") or []) for n in module.get("nodes") or [])
    thin_counts = _assignment_thin_counts(assignment_map)

    intermediate["validation"] = report.to_dict()
    intermediate["repair_count"] = repair_count
    intermediate["thin_slot_counts"] = thin_counts
    intermediate["out_of_scope_counts"] = count_out_of_scope(assignment_map)
    intermediate["content_preserve_suspect_count"] = len(report.content_preserve_suspects)
    intermediate["stats"] = {
        "elapsed_seconds": round(elapsed_all, 2),
        "calls": stats.calls,
        "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "total_tokens": stats.total_tokens,
        "estimated_cost_cny": round(cost, 4),
        "stage_logs": stats.stage_logs,
        "failures": stats.failures,
        "retries": stats.retries,
    }
    intermediate["counts"] = {
        "nodes": len(module.get("nodes") or []),
        "npcs": len(module.get("npcs") or []),
        "endings": len(module.get("endings") or []),
        "agenda": len(module.get("agenda") or []),
        "checks": check_count,
        # 线索账本的地基。0 ≠ "还没做"，看上面那行「事实寻址」的分解。
        "facts": fact_stats["facts_total"],
        "facts_from_checks": fact_stats["on_success"],
        "kp_guidance_keys": len(module.get("kp_guidance") or {}),
        "key_facts": len((module.get("kp_truth") or {}).get("key_facts") or []),
        "player_intro_items": thin_counts.get("player_intro", 0),
        "opening_items": thin_counts.get("opening", 0),
        "meta_items": thin_counts.get("meta", 0),
        "content_preserve_suspects": len(report.content_preserve_suspects),
    }
    # 🔴 循环结束，交出**最好的那一版**
    if len(best_report.all_errors()) < len(report.all_errors()):
        print(
            f"\n🔴 自修没能变好：最后一版 {len(report.all_errors())} 处问题，"
            f"回退到最好的那一版（{len(best_report.all_errors())} 处）",
            flush=True,
        )
        module = best_module
        report = best_report
        assignment_map = best_map

    intermediate["repair_rounds"] = repair_count
    intermediate["success"] = report.ok

    # 写产物（模组资料/，gitignored）
    out_structured.parent.mkdir(parents=True, exist_ok=True)
    out_structured.write_text(
        json.dumps(module, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_intermediate.write_text(
        json.dumps(intermediate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_report.write_text(
        json.dumps(
            {
                "success": report.ok,
                "repair_count": repair_count,
                "report": report.to_dict(),
                "counts": intermediate["counts"],
                "thin_slot_counts": thin_counts,
                "out_of_scope_counts": intermediate["out_of_scope_counts"],
                "stats": intermediate["stats"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("\n=== 完成 ===", flush=True)
    print(f"wrote: {out_structured}", flush=True)
    print(f"wrote: {out_intermediate}", flush=True)
    print(f"wrote: {out_report}", flush=True)
    print(f"success: {report.ok}", flush=True)
    print(f"repair_count: {repair_count}", flush=True)
    print(f"counts: {intermediate['counts']}", flush=True)
    print(f"thin_slot_counts: {thin_counts}", flush=True)
    print(
        f"content_preserve_suspects: {len(report.content_preserve_suspects)}",
        flush=True,
    )
    print(
        f"calls={stats.calls} elapsed={elapsed_all:.1f}s "
        f"tokens={stats.total_tokens} cost≈¥{cost:.4f}",
        flush=True,
    )
    for log in stats.stage_logs:
        print(
            f"  · {log['label']}: {log['elapsed_s']}s "
            f"in={log['prompt_tokens']} out={log['completion_tokens']}",
            flush=True,
        )

    # 校验不过不假装成功
    return 0 if report.ok else 2


def default_paths(extract: Path) -> tuple[Path, Path, Path]:
    name = extract.name
    base = name[: -len(".裸抽取.json")] if name.endswith(".裸抽取.json") else extract.stem
    parent = extract.parent
    return (
        parent / f"{base}.structured.json",
        parent / f"{base}.组装中间态.json",
        parent / f"{base}.校验报告.json",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预处理管线组装层 + 校验闭环")
    parser.add_argument("--extract", type=Path, required=True, help="裸抽取 JSON")
    parser.add_argument("--relations-pass1", type=Path, required=True)
    parser.add_argument("--relations-pass2", type=Path, required=True)
    parser.add_argument("--source-txt", type=Path, required=True, help="模组原文 txt")
    parser.add_argument(
        "--example",
        type=Path,
        default=None,
        help="格式范例 structured.json（仅取骨架，内容不进产物）",
    )
    parser.add_argument("--out", type=Path, default=None, help="输出 structured.json")
    parser.add_argument("--out-intermediate", type=Path, default=None)
    parser.add_argument("--out-report", type=Path, default=None)
    args = parser.parse_args(argv)

    structured, intermediate, report = default_paths(args.extract)
    if args.out:
        structured = args.out
    if args.out_intermediate:
        intermediate = args.out_intermediate
    if args.out_report:
        report = args.out_report

    return run_pipeline(
        extract_path=args.extract,
        pass1_path=args.relations_pass1,
        pass2_path=args.relations_pass2,
        source_txt=args.source_txt,
        example_path=args.example,
        out_structured=structured,
        out_intermediate=intermediate,
        out_report=report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
