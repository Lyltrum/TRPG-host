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
