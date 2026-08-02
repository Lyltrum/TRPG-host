"""技能名归一（运行时唯一口径）。

## 为什么要单独一个模块

技能名归一此前散在**三处**，而且互不相同：

| 层 | 位置 | 条目数 |
|---|---|---|
| 执行层 | `tools._SKILL_SYNONYMS` | 3 |
| 组装层 | `scripts/module_probe/validate_module.SKILL_ALIASES` | 26 |
| **运行时护栏** | `check_guard` | **0（没有）** |

后果是真人实测 2026-07-31 撞见的那个 bug（exec/12 #32）：《追书人》的节点把
检定点标成「侦查」，裁决器按被教对的规范名发起「侦察」，护栏做精确字符串
比较 → 拦掉 → **玩家明说"我要过一下侦查"也不掉骰子，而且静默无提示**。

裁决器发的名字和模组标注的名字，本来就来自两个不同的作者（一个是模型按
规则表说话，一个是人工/管线结构化剧本时随手写的），指望它们逐字相同是不
现实的。

## 🔴 不变量：护栏不该拦住执行层能解析的技能名

护栏（`check_guard`）和执行层（`tools._resolve_skill_target`）必须用**同一套**
归一口径。护栏比执行层严，就会出现"裁决判对了、执行层也认得，却在中间被
拦掉"——而且拦截理由只进 issue 日志，玩家侧完全静默。

组装层那份表条目更多、面向"把剧本里的野写法修成规则表规范名"，用途不同，
暂时留在 scripts/ 不动；但**运行时这两处必须共用本模块**。
"""

from __future__ import annotations

#: 口语/异体写法 → COC7 规则表规范名。发现一个补一个。
#: 真人实测来源：「侦查」（exec/09 #6、exec/12 #32）、「观察」（exec/09 #6）、
#: 「闪躲」（exec/10 #6）。
SKILL_SYNONYMS: dict[str, str] = {
    "侦查": "侦察",
    "观察": "侦察",
    "闪躲": "闪避",
}


def canonical_skill_name(name: str | None) -> str:
    """归一到规范名：去空白 + 同义词替换。

    只做**确定性**替换，不做模糊匹配——模糊匹配会把"驾驶"这种大类前缀
    瞎猜成某个子类（exec/09 #5 已经踩过，那次的结论是宁可报错列候选）。
    """
    stripped = (name or "").strip()
    return SKILL_SYNONYMS.get(stripped, stripped)


def match_key(name: str | None) -> str:
    """用于比较的键：归一 + 去空格 + 小写。护栏两侧都要用它。"""
    return canonical_skill_name(name).replace(" ", "").lower()


# ── 技能指向 id 化（exec/17 (B)）────────────────────────


def skill_id_catalog(ruleset) -> dict[str, str]:  # noqa: ANN001 — RulesetRead，避免循环 import
    """白名单：`id → 展示名`。技能用规则表 id，属性用属性 key（STR/CON…）。

    这是裁决器**唯一**被允许写进 `checks[].skill_id` 的取值集合。92 个技能
    id + 9 个属性 key 是个封闭集合，随规则版本变、不随模组变——让模型从里面
    挑一个，比让它写中文名再由代码去猜它想说哪个可靠得多（exec/17）。
    """
    catalog = {spec.id: spec.name for spec in ruleset.skills}
    catalog.update({attr.key: attr.label for attr in ruleset.attributes})
    return catalog


def resolve_skill_id(ruleset, skill_id: str | None) -> str | None:  # noqa: ANN001
    """id → 展示名。不在白名单里返回 None（调用方报错，不猜）。"""
    key = (skill_id or "").strip()
    if not key:
        return None
    catalog = skill_id_catalog(ruleset)
    if key in catalog:
        return catalog[key]
    # id 大小写不敏感：模型偶尔写 "Spot-Hidden"
    lowered = {k.lower(): v for k, v in catalog.items()}
    return lowered.get(key.lower())
