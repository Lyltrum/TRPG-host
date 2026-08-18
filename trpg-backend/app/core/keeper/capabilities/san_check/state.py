"""模组标注的理智检定点：局面块 + 已触发记账。

## 🔴 这是**反向**护栏，而且它只能是提醒

`skill_check` 的护栏管的是「模组没标注的不许掷」；反过来「模组标注了却一次
都没触发」此前没有任何东西管。真人实测（`exec/31 #73`）：导入的模组 23 个
节点里只有 1 处 `kind="san"` 检定点，玩家**进去了**，裁决器当轮只发了一次
逃跑的敏捷对抗，SAN 一次没起——全局唯一那次理智检定反而掷在没标注的地方。

**执行侧刻意不做成代码强制掷。** COC7 里触发点是「目睹的那一刻」，代码判不了
「他看见了没有」；而 SAN 写进角色卡不可撤回，掷早了还会撞上规则里「同一来源
不重复检定」。所以按 `exec/20` 的分层，这一条是**触发条件由模型判、代码只
提供数值 → 概率性改进**，汇报时说"已改善"，不说"已修复"。

已触发记账（`SAN_POINTS_FIRED_KEY`）存在的理由：没有它，同一条标注会被提醒
到这一局结束，模型照做就是重复扣 SAN——比不提醒更糟。

## 🔴 2026-08-15：不再按位置过滤

原来注入与记账**两头都按「玩家所在节点」**。08-14 回归实测量出来那是死路：
林中屋唯一那条 `kind="san"` 挂在 `migo-cover-blown` 上，那个节点 `exits: []`
**且没有任何节点指向它**——图上的孤岛，玩家用任何走法都站不上去。整块提示
一次都没渲染过，28 轮 SAN 掷了 0 次。**不是模型不听话，是没给它看。**

现在：注入列全模组未触发的标注、**只给数值不给标题**（标题本身剧透），
记账改成**按损失数值回匹**。两头必须一起改——只改注入的话标注永远标不掉。
`occupied_node_ids` / `san_points_at` / `fired_refs_at` 三个按节点取的辅助
随之删除，它们是这次改动产生的孤儿。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ModuleCheck, ScenarioModule, iter_all_nodes
from app.core.keeper.contract.registry import SituationContext

SAN_POINTS_FIRED_KEY = "已触发理智检定点"

#: 最近几次理智检定各是为什么掷的（`SanCheckRequest.reason` 原文）。
#: 值是 list[str]，最新的在最后。
RECENT_SAN_KEY = "最近理智检定"

#: 留几条。同一场遭遇里理智检定本来就该稀少，留太多会把很久以前的事翻出来
#: 当"刚掷过"。真机那次的失控是连续 3 拍，4 条足够看见它。
_RECENT_SAN_LIMIT = 4

#: 模组里表示"这条检定点是理智检定"的 kind（`ModuleCheck.kind` 的封闭取值之一）。
SAN_KIND = "san"


def san_point_ref(node_id: str, index: int) -> str:
    """一处理智检定点的引用。

    🔴 `ModuleCheck` 没有 id，只能用「节点 id + 它在 checks 里的序号」定位。
    这不是"自由文本当标识符"——两半都是代码算出来的，模型碰不到它。
    """
    return f"{node_id}#{index}"


def load_fired_san_points(keeper_state: dict | None) -> list[str]:
    """解析已触发过的检定点引用（存储形态同 `已触发议程`：逗号分隔字符串）。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(SAN_POINTS_FIRED_KEY)
    if raw is None or raw == "":
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def load_recent_san_reasons(keeper_state: dict | None) -> list[str]:
    """解析最近几次理智检定的理由。脏数据一律当没有——它只是参考材料。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(RECENT_SAN_KEY)
    if not isinstance(raw, list):
        return []
    return [text for item in raw if (text := str(item).strip())]


def record_san_reason(recent: list[str], reason: str) -> list[str]:
    """记一次。返回**新表**（同 `record_attempt`：调用方要整列写回）。

    空理由不记：记了也没法帮模型判断"是不是同一个来源"，只会占掉一格。
    """
    text = (reason or "").strip()
    if not text:
        return list(recent)
    return [*recent, text][-_RECENT_SAN_LIMIT:]


def format_recent_san(context: SituationContext) -> str:
    """局面块：最近为什么掷过理智，附「同一来源不重复」那条纪律。

    ## 🔴 为什么是注入而不是拦截（2026-08-18 真机）

    实测连着三拍为**同一具尸体**掷了三次理智：「目睹被近距离枪杀」→「目睹
    爆头后复活起身」→「目睹复活后蹒跚走向大门」。COC7 里同一来源一场遭遇
    只掷一次，规则 3 也写着——但**"已经为这个来源掷过了"从来没进过它的上下文**，
    跟 `skill_check` 的「本地检定次数」是同一个病。

    `executor.py` 里那道「一拍之内只掷一次」的门拦不住这个：它按**拍**分界，
    而这三次各自跟在一句新的玩家发言后面，分属三拍。那道门的注释里其实已经
    写出了自己的假设——「拦掉之后玩家下一次发言就能再掷（真的升级了，下一拍
    照样掷得出）」。08-16 的数据里"跟在新发言后"就是合法；08-18 的数据里三次
    全跟在新发言后、**全是同一个来源**。同一个判据在两份数据上给出相反结论
    ⇒ 不能再加一道同形状的门。

    🔴 **`reason` 在这里只当展示内容，不当标识符。** 拿它做 key 去 dedup 才是
    「用自由文本当标识符」；把原文摆到模型眼前、由它判"是不是同一个来源"，
    正是「能确定化的是判断的输入，不是判断本身」。
    """
    recent = load_recent_san_reasons(context.keeper_state)
    if not recent:
        return ""
    lines = "\n".join(f"- {text}" for text in recent)
    return (
        "【最近已经掷过的理智检定】\n"
        + lines
        + "\n**同一个来源不要重复检定**：上面这些已经掷过了，"
        "同一个东西后来又动了一下、又靠近了一点、又被看清了一点，**都还是它**，"
        "不要再为它发起理智检定——直接按已有结果往下写。\n"
        "**换成新的来源照掷**：另一个怪物、另一具尸体、另一件此前没见过的事，"
        "那是新的一次，该掷就掷。"
    )


def render_recent_san(context: SituationContext) -> str:
    """注册进局面块的 situation 钩子。"""
    return format_recent_san(context)


def _same_loss(left: str | None, right: str | None) -> bool:
    """两个损失表达式是不是同一个。只做去空白 + 忽略大小写（`1D6` vs `1d6`）。

    🔴 这**不是**自由文本当标识符：两侧同源——局面块把模组的数值原样列出来，
    规则要求模型**照抄**，回来的就该是同一个串。归一只覆盖抄写时的大小写抖动，
    不做任何同义词映射。
    """
    return (left or "").strip().casefold() == (right or "").strip().casefold()


def match_san_point(
    module: ScenarioModule,
    keeper_state: dict | None,
    loss_on_success: str | None,
    loss_on_failure: str | None,
) -> str | None:
    """把一次已发起的理智检定回匹到模组标注上，返回它的 ref。匹配不上返回 None。

    🔴 **为什么记账口径必须跟着注入口径一起改**：注入改成全局之后，原来那套
    「按玩家所在节点标掉」几乎永远是空操作——玩家站不到遭遇节点上，于是
    标注永远标不掉、提示**每轮重复**，模型照做就是重复扣 SAN，比不提醒更糟。
    加了字段没有消费方是一种缺陷，**改了口径只改一半**是它的镜面。

    按数值回匹，匹配不上就**不标**（显式降级，由调用方记 issue）——不猜、
    也不按顺序随便标一条：多条标注数值不同时，标错等于把另一条也吞掉。
    """
    for ref, check in unfired_san_points(module, keeper_state):
        if _same_loss(check.on_success, loss_on_success) and _same_loss(
            check.on_failure, loss_on_failure
        ):
            return ref
    return None


def unfired_san_points(
    module: ScenarioModule, keeper_state: dict | None
) -> list[tuple[str, ModuleCheck]]:
    """**全模组**还没触发过的理智检定点，保序。返回 (ref, check)。

    🔴 **不按位置过滤**（2026-08-15 改）。原来只列"玩家此刻所在节点上"标注的，
    而 08-14 回归实测量出来：林中屋唯一那条 `kind="san"` 挂在 `migo-cover-blown`
    上，那个节点 `exits: []` **且没有任何节点指向它**——节点图上的孤岛，玩家
    用任何走法都站不上去。于是整块提示**一次都没渲染过**，28 轮 SAN 掷了 0 次。
    不是模型不听话，是没给它看。

    ⚠️ 这是**同一个错误的第四张脸**（前三次都在收尾门：分母含玩家去不了的
    节点 / 分子没人写 / 分母含永远揭不开的配对）：**判据用了一个不对应玩家
    实际处境的量**。这次不再试图修"玩家到不到得了那个节点"——遭遇类节点本来
    就不是玩家会站上去的地方，那条路走不通。
    """
    fired = set(load_fired_san_points(keeper_state))
    out: list[tuple[str, ModuleCheck]] = []
    for node in iter_all_nodes(module.nodes):
        for index, check in enumerate(node.checks):
            if check.kind != SAN_KIND:
                continue
            ref = san_point_ref(node.id, index)
            if ref not in fired:
                out.append((ref, check))
    return out


def format_san_points(
    module: ScenarioModule,
    keeper_state: dict | None,
    players: tuple[tuple[str, str], ...],
) -> str:
    """局面块正文。没有待触发的检定点就返回空串——整块不渲染。

    🔴 **只给数值，不给节点标题**（用户 2026-08-15 拍板的 A 方案）。
    标题本身就是剧透——遭遇类节点的标题往往把即将发生的事直接写在上面，
    光是列出来就等于提前告诉模型剧本要发生什么。而模组标注真正提供的是**数值**：
    「什么时候该掷」本来就是 KP 的判断（真人 KP 也是看见怪物才喊掷），
    代码判得了"人在哪"，判不了"他看见了没有"。

    `players` 仍在签名里但不再参与过滤——保留是因为 `SituationBlock` 的渲染
    钩子签名固定。**不删参数、也不假装用它**，理由写在这里。
    """
    points = unfired_san_points(module, keeper_state)
    if not points:
        return ""
    lines = []
    for _ref, check in points:
        difficulty = f"（{check.difficulty}）" if check.difficulty else ""
        lines.append(
            f"- {difficulty}成功损失 {check.on_success or '—'}／失败损失 {check.on_failure or '—'}"
        )
    return (
        "这个模组标注了下面这些理智检定点，本局还没掷过（**不说在哪触发——"
        "那由你判断**）。\n"
        "玩家这一轮**目睹**足以动摇理智的东西时，必须在 `san_checks` 里发起，"
        "`loss_on_success`/`loss_on_failure` **照抄**下面的数值；还没看见就先别掷。\n"
        + "\n".join(lines)
    )


def render_san_points(context: SituationContext) -> str:
    """注册进局面块的 situation 钩子。"""
    return format_san_points(context.module, context.keeper_state, context.players)
