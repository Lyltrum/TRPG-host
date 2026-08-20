"""导入 job 的状态机（`exec/29` 第 5 步）。

## 为什么 job 得存在

管线实测 **~71 次 LLM 调用 / 5–26 分钟 / ¥0.35**。这个量级决定它不能是一个
HTTP 请求：连接挂 20 分钟，任何抖动都让用户既不知道成没成、也不知道钱花没花。

## 🔴 分阶段不是为了好看

**每个阶段的失败对用户意味着完全不同的下一步**——取文失败是"换个文件 / 这是
扫描件"，校验失败是"这份模组转不了"。混成一个 `failed` 等于什么都没说，而
「失败的唯一出口是拒绝」的前提是那条拒绝理由**可执行**。

## 🔴 剧透约束在这里靠 schema 执行，不靠自觉

job 的状态与报告是**唯一跨到前端的东西**。所以这里刻意**没有**一个自由的
`stats: JSON` 字段——自由 JSON 能装下任何东西，包括模组正文。取而代之的是
一组**显式的整数列**加一个**封闭集合**的失败类别列表：schema 表达不了的东西
才漏不出去（同「保密靠拿不到，不是请你别说」）。

改这里之前先想清楚：**新加的字段能不能装下一句剧透？** 能就别加。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

# ── 终态与中间态 ──────────────────────────────────────

#: 排队中——已收下文件，还没开始烧钱。
STATUS_PENDING: Final = "pending"
#: 正在跑。**只有这个状态需要一个活着的进程**，见 `stale_running_reason`。
STATUS_RUNNING: Final = "running"
#: 成功注册成一个可玩的 scenario。
STATUS_SUCCEEDED: Final = "succeeded"
#: 拒绝。`failure_reason` 必须可执行。
STATUS_FAILED: Final = "failed"
#: 后端重启把它冲掉了。跟 `failed` 分开——**这不是模组的问题**，
#: 混在一起会让用户以为自己的模组转不了。
STATUS_INTERRUPTED: Final = "interrupted"

TERMINAL_STATUSES: Final = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_INTERRUPTED})
ALL_STATUSES: Final = frozenset(
    {STATUS_PENDING, STATUS_RUNNING, *TERMINAL_STATUSES},
)

# ── 阶段 ──────────────────────────────────────────────

#: 顺序即进度。跟 `pipeline.convert` 里那五步一一对应，**多一步都不要有**：
#: 阶段是给用户看的，不是内部调用图。
STAGES: Final[tuple[str, ...]] = (
    "received",  # 收下文件
    "extracting",  # 取文（PDF/DOCX/DOC/TXT → 文本）
    "probing",  # 裸抽取
    "relating",  # 关系发现
    "assembling",  # 组装 + 校验 + 自修
    "registering",  # 落库、注册成可玩模组
)

#: 校验器的硬失败类别（`ValidationReport.all_errors` 的方括号前缀）。
#:
#: 🔴 **这是个封闭集合，报告层只许出现这里面的词。** 让它封闭正是剧透约束的
#: 执行方式之一：错误原文里带着实体 id 和正文片段，只有类别是安全的。
FAILURE_KINDS: Final[tuple[str, ...]] = (
    "schema",
    "ref",
    "skill",
    "orphan",
    "leak",
    "facts",
    "thin_slot",
    "secret_public",
    "structure",
    "trace",
    "numeric",
    "reach",
)


#: 🔴 **哪些硬失败可以降级交付**（2026-08-20，用户拍板）。
#:
#: 这条线的定位是「**只能成功不能失败**」——遇到失败先找更窄的修法，而**最窄的
#: 那一档就是"照样交付，但把哪里有问题标出来"**。
#:
#: 分档不是拍脑袋，有实测支撑：`模组资料/` 里的**复足**与**神秘渡轮**当年就带着
#: `trace` 问题进了库，两份都跑过好几局真机，从没出过溯源相关的问题
#: （见 `tests/test_import_regression_corpus.py` 里记的期望值）。一份能玩的模组
#: 不该因为两个没有原文依据的节点被整个扔掉。
#:
#: **不在这张表里的一律仍然拒绝**，其中两类是用户明确同意保留硬门的：
#:
#: - `leak` —— 剧透第一性，那是这条线三层判据的第一层；
#: - `content_preserve` —— 改到连原文都对不上，那不是"修好了"，是编了一份。
#:
#: 其余没列进来的（`schema` / `ref` / `structure` / `facts` / `numeric`）是
#: **结构性损坏**：产物本身不自洽，主持人跑到那儿会直接撞上，标注也救不了。
DEGRADABLE_FAILURE_KINDS: Final[frozenset[str]] = frozenset(
    {
        # 节点没有原文依据 ⇒ 删掉它比留着更安全（留着 = 玩家走进去看模型现编）
        "trace",
        # 有一幕玩家可能走不到 ⇒ 标出来，主持人自己会圆
        "reach",
        # 有片段没归位 ⇒ 少一段内容，不影响其余部分能不能跑
        "orphan",
        # 开场信息过多 ⇒ 是节奏问题不是正确性问题
        "thin_slot",
        # 技能名归不了一 ⇒ 那条检定去掉，那一幕照样主持
        "skill",
    }
)


def is_degradable(kinds: Sequence[str]) -> bool:
    """这批失败能不能降级交付。

    🔴 **空集合返回 False**：一次没有任何类别的失败说明分类器本身没认出来，
    那时"能降级"是个没有依据的结论。缺数据要显式失败，不静默放行。
    """
    if not kinds:
        return False
    return all(k in DEGRADABLE_FAILURE_KINDS for k in kinds)


def stage_index(stage: str) -> int:
    """阶段序号；未知阶段返回 -1（**不当成 0**——那会让它看起来刚开始）。"""
    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def stale_running_reason() -> str:
    """启动清扫时写给用户的话。

    🔴 **它必须是"重试"而不是"失败"**：这不是模组的问题，是我们的进程没了。
    """
    return "后端重启了，这次转换没跑完。请重新导入这份模组（不会重复扣费）。"


def normalize_failure_kinds(errors: list[str]) -> list[str]:
    """从 `all_errors` 里只提取方括号类别，**丢掉一切正文**。

    🔴 这是剧透约束的执行点。错误原文长这样：

        [numeric] node 'mi-go' 的数值 '701d6+1' 在原文里找不到——疑似凭空生成

    实体 id、数值、乃至半句原文都在里面。跨到前端的只能是 `numeric` 这一个词。
    不在封闭集合里的前缀**一律丢弃**，不做"未知类别"兜底——那个兜底会把任意
    字符串放行。
    """
    kinds: list[str] = []
    for err in errors:
        if not err.startswith("["):
            continue
        head, _, _rest = err[1:].partition("]")
        if head in FAILURE_KINDS and head not in kinds:
            kinds.append(head)
    return sorted(kinds)
