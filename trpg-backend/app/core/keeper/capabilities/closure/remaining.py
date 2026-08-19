"""「这一局还剩多少内容」的几个计数，每轮摆到裁决器眼前。

## 为什么是局面块

同 `format_endings_status` 的形状：**把该判断的东西摆到眼前，比在规则里多写
一句可靠**。这些数回答的是「这一局走到哪了」——对着模组自己定义的格子数，
**不给模组的结构打分**。（那七个被量废的候选问的是另一件事：「这份模组结构
好不好」，那是对内容下判断，做不到。）

## 🔴 只有前两个数是门，后两个数是纯参考（2026-08-13 定）

「未揭开配对」和「未触发的一次性议程」有 id、有记账、**分母是玩家真能走到
底的**，它们做收尾的门槛。另外两个不做：

- **「没去过的地方」的分母永远见不了底**：它数的是扁平展开的全部节点，而玩家
  位置只落在地点类节点上。林中屋 23 个节点里只有 14 个是地点，其余是物品/
  遭遇/线索/事件——**走遍每个地点，这个数最少也只到 9**。它当过门槛，于是
  「三个数都见底」在结构上永远不可能成立，开放式模组永远等不到落幕。
  （按 `kind` 过滤修不了：`kind` 是自由文本，各模组写法完全不同；`exits` 图
  也替代不了，林中屋 14 个地点里 9 个没有入边。两条路都验过。）
- **「无进展轮数」问的是相反的问题**：它大 = 这桌人在原地打转，那时候该**推**
  （给线索 / 让事件闯进来），不是该**收**。同一个信号不能同时当刹车和油门。

## 🔴 缺数据要显式降级，不许报 0

导入进来的模组曾经**根本不产 `facts` / `visibility_pairs`**，于是「未揭开配对」
对它们恒为 0——而 0 跟"全都揭开了"长得一模一样，正好把收尾门推向放行。
**缺数据必须说出来**（"这份模组没有配对数据"），这是明令禁止的静默兜底那一族。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import (
    ScenarioModule,
    iter_all_nodes,
    reachable_visibility_pairs,
)
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.progress_state import (
    is_pair_revealed,
    load_fired_agenda,
    load_revealed_clues,
    load_stalled_turns,
    load_visited_nodes,
)


def unrevealed_pair_count(module: ScenarioModule, keeper_state: dict | None) -> int | None:
    """还有多少条**玩家揭得开的**线索配对没揭开。没有这类配对时返回 None，不是 0。

    🔴 分母是 `reachable_visibility_pairs`，不是全部配对：真相侧不指向节点的
    那些在结构上永远揭不开（林中屋 6 条里有 3 条指向 NPC id），留在分母里
    等于这道门永远过不去。理由与判据见那个函数的 docstring。
    """
    reachable = reachable_visibility_pairs(module)
    if not reachable:
        return None
    revealed = load_revealed_clues(keeper_state)
    return sum(1 for pair in reachable if not is_pair_revealed(revealed, pair.id))


def unfired_agenda_count(module: ScenarioModule, keeper_state: dict | None) -> int | None:
    """还有多少条 `once` 议程没触发。没有议程表时返回 None。

    只数 `once=True` 的：`once=False` 的事件可以反复发生，"还没发生过"对它
    不成立，拿它当"内容没跑完"的证据会让对局永远收不了尾。
    """
    once_events = [e for e in module.agenda if e.once]
    if not once_events:
        return None
    fired = set(load_fired_agenda(keeper_state))
    return sum(1 for event in once_events if event.id not in fired)


def unvisited_node_count(module: ScenarioModule, keeper_state: dict | None) -> int | None:
    """还有多少个剧本节点没去过。没有节点的模组返回 None。

    🔴 **这个数不是门**（见模块开头）：分母含物品/遭遇/线索这类"去不了"的
    节点，它天然到不了 0。只作参考，渲染时也要把这句话写给模型看。

    🔴 口径是**扁平遍历**（`iter_all_nodes`）：子节点有 `sub_node`（单数）与
    `sub_nodes`（复数）**两个**字段，少走一个就少数一批。只数顶层更糟——
    "林中屋只有 4 个 node"那个假数字就是这么来的，我据此下过大结论。
    """
    all_nodes = iter_all_nodes(module.nodes)
    if not all_nodes:
        return None
    visited = set(load_visited_nodes(keeper_state))
    return sum(1 for node in all_nodes if node.id not in visited)


def _line(label: str, remaining: int | None, total: int, missing_note: str) -> str:
    if remaining is None:
        return f"- {label}：{missing_note}"
    return f"- {label}：还剩 {remaining} / 共 {total}"


#: 连续几轮没有新进展之后，那一行从「参考信息」升级成「本轮的硬要求」。
#:
#: 🔴 **这个信号此前没有任何消费方**（2026-08-14 实测：一局跑到 `无进展轮数=26`，
#: 而它只是局面块里一句陈述）。症状是玩家连说四轮「继续走」，拿到四段越来越像
#: 的洞穴描写——最后两拍**逐字相同**。模型收不了场，因为那条即兴出来的窄洞
#: 没有长度、没有终点，而"该给推力了"这件事没人告诉它。
#:
#: 取 3：真人 KP 在玩家第二、三次重复同一句话时就会动手。再往上等，玩家已经
#: 在打转里待够久了。
STALL_PUSH_THRESHOLD = 3


def _stalled_line(stalled: int) -> str:
    """无进展那一行。到阈值之后**换成一条硬要求**，不再只是陈述一个数字。

    🔴 **确定性只到"触发"这一层**：`stalled` 是代码算的、阈值是代码判的，
    所以这句要求一定会出现在 prompt 里；但"这一轮到底有没有给出推力"是语义，
    代码判不了 ⇒ 按 `exec/20` 的口径这仍是**概率性改进**，汇报时不说"已修复"。
    """
    if stalled < STALL_PUSH_THRESHOLD:
        return (
            f"- 已经 {stalled} 拍没有新进展（没去新地方、没揭开新线索、"
            "世界也没往前走一步）"
            "——这说明这桌人在打转，该给推力（线索、事件、NPC 上门），不是该收场"
        )
    return (
        f"🔴 **已经 {stalled} 拍没有新进展了（没去新地方、没揭开新线索、"
        "世界也没往前走一步）——"
        "这一轮必须让局面动起来，不许再写一段「继续往前走」的同质描写。**"
        "从下面挑一个真的落地：\n"
        "  ① **让路到头**：走到尽头 / 塌方 / 岔成两条 / 通到一个新地方（那就建一个新位置）；\n"
        "     （这一条是「没人指定方向」时的走法。玩家已经点名要去哪了，按规则 4e-2 办——\n"
        "     给检定或给代价，不要用世界设定取消掉他的选择。）\n"
        "  ② **让事件闯进来**：追的东西追上了、手电没电了、NPC 喊了一声、听见上面有动静；\n"
        "  ③ **跳过过程**：「你们走了二十分钟，前面豁然开朗」——别一步一步陪着挪；\n"
        "  ④ **给一条新线索**（配得上这个地方的，不要凭空发明关键真相）。\n"
        "  这**不是**该收尾的信号——打转要推，不要收场"
        "（`story_ran_its_course` 照旧看上面两行门槛）。"
    )


def format_remaining_content(module: ScenarioModule, keeper_state: dict | None) -> str:
    """渲染两行门槛 + 两行参考。参考那两行跟模组有没有配对数据无关，照样渲染。

    🔴 **哪几行是门槛必须写在文本里**：模型看到的是一串长得一样的数字，不写
    就只能靠它自己猜哪个算数——而它猜错的那一版正是被真人实测打回来的那版。
    """
    pairs = unrevealed_pair_count(module, keeper_state)
    agenda = unfired_agenda_count(module, keeper_state)
    nodes = unvisited_node_count(module, keeper_state)
    stalled = load_stalled_turns(keeper_state)
    return "\n".join(
        [
            "【收尾门槛：下面两行都归零才可以写 story_ran_its_course】",
            _line(
                "未揭开的线索配对",
                pairs,
                # 分母跟 `unrevealed_pair_count` 用**同一个**集合。分子分母来自
                # 两个不同的集合，是「报少了多少之前先确认两边同一个单位」那条
                # 判据里已经用错过三次的形态。
                len(reachable_visibility_pairs(module)),
                "这份模组没有玩家揭得开的配对数据，据此判断不了还剩多少线索",
            ),
            _line(
                "未触发的议程（只数一次性的）",
                agenda,
                len([e for e in module.agenda if e.once]),
                "这份模组没有一次性议程",
            ),
            "【下面两行只是参考，不是收尾依据】",
            _line(
                "没去过的地方",
                nodes,
                len(iter_all_nodes(module.nodes)),
                "这份模组没有节点",
            )
            + ("（分母含物品、遭遇这类去不了的节点，它天然到不了 0）" if nodes is not None else ""),
            _stalled_line(stalled),
        ]
    )


def render_remaining_content(context: SituationContext) -> str:
    return format_remaining_content(context.module, context.keeper_state)


def format_key_facts(module: ScenarioModule) -> str:
    """模组的核心真相清单（`kp_truth.key_facts`），收尾判断的参照。

    🔴 **它已经在 system prompt 里了**（`render_full` → `render_overview`），这里
    是**第二次**摆——跟 `format_endings_status` 同一个理由：埋在几千字剧本全文
    中间的东西，等于每轮都指望模型自己想起来去翻。真人 KP 判"该收了"看的正是
    这份清单（核心真相揭开了没有），那就把它摆到做这个判断的那一拍眼前。

    ⚠️ 如实说：这是**概率性改进**（`exec/20`）。key_facts 是自由文本，代码数不了
    "揭开了几条"，做不成门槛——门槛只能是有 id 有记账的那两个数。

    只用 `module`：真相来自剧本，不随对局状态变。
    """
    facts = [f.strip() for f in module.kp_truth.key_facts if f.strip()]
    if not facts:
        return ""
    return "\n".join(f"- {fact}" for fact in facts)


def render_key_facts(context: SituationContext) -> str:
    return format_key_facts(context.module)
