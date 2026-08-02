"""per-player 位置（exec/14 P5.2 的共用地基）。

## 为什么位置要按人存

私密性的根源只有一个：**你不在场，所以你不知道**（exec/18 定稿）。①分头
探索、②潜行躲藏、⑥玩家主动私密三件事共用同一套 per-observer 投递，而投递
的分组依据就是位置——位置不按人存，这三件事一件都做不了。

## 与房间级「当前场景节点」的关系

`scene_state.CURRENT_NODE_KEY` 保留原样，语义收窄为**大部队所在**：
- 本表（`PLAYER_LOCATION_KEY`）记的是**逐人**位置，只在有人被显式定位过时才有条目；
- 查一个人的位置：先查本表，查不到**回落到房间级指针**。

这个回落是**有定义的默认值**，不是静默兜底：「没有被单独定位过的调查员，
就跟大部队在一起」是这个模型里唯一说得通的语义，而且它让 P5.2 之前建的
房间（只有房间级指针）行为逐字不变。**查不到位置返回 None**——上层不得
把 None 当成"跟谁都在一起"，全 None 时整桌就是一组（见 group_players）。

## 存储形态

跟 `visibility.py` 同一套：keeper_state 里一个逗号分隔的字符串，
`player_id@node_id`。这个键由 `movement` 能力通过注册表的 `reserved_state_keys` 钩子声明出去，
LLM 的 `state_updates` 改不动它。

## 🔴 为什么它没有跟着 `movement` 能力一起搬走（exec/27 阶段 3）

「谁在哪」是**整局的共享空间状态**：叙事分组（`agent`）、讨论区投递
（`controller/ws.py`）、检定护栏（按掷骰那个人所在的节点判定）都在读它。
判据同 `phase.py`：**共享的状态与它的读写归 runtime，用它做裁决的字段与
执行归能力。** 所以这里留下"位置是什么、怎么读、怎么写"，而
`current_node_id` / `moves` / `hiding` 三个裁决字段、规则 4b/4c、以及
「各自所在」局面块在 `capabilities/movement/`。
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    record_event,
    resolve_character,
)
from app.core.keeper.runtime.scene_state import (
    CURRENT_NODE_KEY,
    SCENE_NAME_KEY,
    load_current_node_id,
)
from app.models.room import Player, Room

PLAYER_LOCATION_KEY = "玩家位置"

#: 潜行/躲藏中的调查员（exec/18 ②）。「在场但不可见」——他照常**听得见**这里
#: 发生的一切（所以不把他从位置分组里摘出去），但他自己的行动不会广播给同处
#: 的其他人。存 player_id 的逗号串。
HIDDEN_PLAYERS_KEY = "隐匿玩家"


def load_player_locations(keeper_state: dict | None) -> dict[str, str]:
    """解析 player_id → node_id。保序、去空、后写覆盖先写。"""
    if not keeper_state:
        return {}
    raw = keeper_state.get(PLAYER_LOCATION_KEY)
    if raw is None or raw == "":
        return {}
    out: dict[str, str] = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part or "@" not in part:
            continue
        player_id, node_id = part.split("@", 1)
        player_id, node_id = player_id.strip(), node_id.strip()
        if player_id and node_id:
            out[player_id] = node_id
    return out


def serialize_player_locations(locations: dict[str, str]) -> str:
    return ", ".join(f"{pid}@{nid}" for pid, nid in locations.items())


def load_hidden_players(keeper_state: dict | None) -> set[str]:
    if not keeper_state:
        return set()
    raw = keeper_state.get(HIDDEN_PLAYERS_KEY)
    if raw is None or raw == "":
        return set()
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def serialize_hidden_players(player_ids: set[str]) -> str:
    return ", ".join(sorted(player_ids))


def location_of(keeper_state: dict | None, player_id: str) -> str | None:
    """这名调查员现在在哪个剧本节点。逐人表优先，回落房间级指针（见模块 docstring）。"""
    located = load_player_locations(keeper_state).get(player_id)
    if located:
        return located
    return load_current_node_id(keeper_state)


def group_players(
    keeper_state: dict | None, player_ids: list[str]
) -> list[tuple[str | None, list[str]]]:
    """把一组玩家按位置分组，返回 [(node_id, [player_id, ...]), ...]。

    保 `player_ids` 的入参顺序（分组顺序 = 各组第一个人出现的顺序），叙事
    分段的顺序因此是确定的、可断言的。位置未知（None）自成一组——**不要**
    把它们并进任何已知位置的组，"不知道他在哪"不等于"他在这儿"。
    """
    groups: dict[str | None, list[str]] = {}
    for pid in player_ids:
        groups.setdefault(location_of(keeper_state, pid), []).append(pid)
    return list(groups.items())


def is_party_split(keeper_state: dict | None, player_ids: list[str]) -> bool:
    """全队是否已分头。单人局恒为 False（退化保证：一个人分不了头）。"""
    return len(group_players(keeper_state, player_ids)) > 1


def scene_changed(
    before: dict | None, after: dict | None, player_ids: list[str] | tuple[str, ...]
) -> bool:
    """本轮有没有人换了地方（`exec/19` 场景切换过渡拍的触发条件）。

    🔴 P5.2：判据从"房间级「当前场景」字段变了没有"改成**逐人位置**比对——
    分头探索后房间不再有单一"当前场景"，而"谁挪了窝"本来就是按人问的问题。
    传进来的是**执行之后**的状态（位置由执行层写库，不是从 decision 字段猜），
    因此也顺带覆盖了 `moves`。

    没有任何一个人两端都有 node_id 时，退回「当前场景」自由文本比较——兼容
    尚未产出 node id 的模组与历史房间。

    从 `agent.narrate` 抽出来（exec/27 阶段 4）：它本来就是个纯函数，埋在那条
    480 行的主流程里既没法单独测，也看不出它其实只依赖三样东西。
    """
    before_nodes = {pid: location_of(before, pid) for pid in player_ids}
    after_nodes = {pid: location_of(after, pid) for pid in player_ids}
    has_node_ids = any(
        before_nodes[pid] is not None and after_nodes[pid] is not None for pid in player_ids
    )
    if has_node_ids:
        return any(before_nodes[pid] != after_nodes[pid] for pid in player_ids)
    prev_scene = (before or {}).get(SCENE_NAME_KEY)
    new_scene = (after or {}).get(SCENE_NAME_KEY)
    return prev_scene is not None and new_scene is not None and prev_scene != new_scene


def format_party_locations(
    module: ScenarioModule,
    keeper_state: dict | None,
    players: list[tuple[str, str]],
) -> str:
    """注入局面块的「各自所在」。

    **全队同处一地、且没人在隐匿时返回空串**——整块不渲染，单人局与未分头的
    多人局 prompt 与 P5.2 之前逐字一致（退化保证）。只有真的分头/有人藏起来了，
    裁决器才需要知道谁在哪、谁看不见谁。
    """
    ids = [pid for pid, _ in players]
    groups = group_players(keeper_state, ids)
    hidden = load_hidden_players(keeper_state)
    if len(groups) <= 1 and not hidden.intersection(ids):
        return ""
    nicknames = dict(players)
    lines: list[str] = []
    for node_id, members in groups:
        node = module.node_by_id(node_id) if node_id else None
        where = f"{node.title}（{node_id}）" if node is not None else (node_id or "（位置未记录）")
        who = "、".join(
            f"{nicknames.get(pid, pid)}（隐匿中）" if pid in hidden else nicknames.get(pid, pid)
            for pid in members
        )
        lines.append(f"- {where}：{who}")
    return "\n".join(lines)


def _drop_stealth_on_move(state: dict, moved_player_ids: set[str]) -> None:
    """离开原地点 → 解除隐匿（exec/19 #46）。就地改 `state`。

    exec/18 ② 早就写明"被发现、主动现身、**离开该地点**都要置回 false"，但那
    三件事此前全靠裁决器写 stealth 自觉完成。试玩实测 2026-08-01：玩家第 6 轮
    躲进街对面的阴影，之后一路垃圾场 → 温室 → 进屋 → 当面摊牌 → 地下室 →
    冲出去报警，到第 26 轮「隐匿玩家」里还挂着他——二十轮之后模型早忘了。

    三件事里**只有"离开该地点"是代码能确定性判断的**（位置变了就是变了），
    所以把这一件收归代码；"被发现/主动现身"仍留给裁决器，那是语义判断。
    这跟 #37 同一条判据：空间状态是地基，它错了投递就跟着错——多人局里一个
    永不解除的隐匿标记意味着队友**永远收不到他的消息**。
    """
    if not moved_player_ids:
        return
    hidden = load_hidden_players(state)
    remaining = hidden - moved_player_ids
    if remaining == hidden:
        return
    if remaining:
        state[HIDDEN_PLAYERS_KEY] = serialize_hidden_players(remaining)
    else:
        state.pop(HIDDEN_PLAYERS_KEY, None)


async def set_current_node_impl(deps: KeeperDeps, node_id: str) -> str:
    """写入调查员当前所在的模组场景节点 id（结构化场景指针）。

    校验 node_id 必须真实存在于剧本节点树（module.node_by_id）——拒绝模型
    编造不存在的 id，与 mark_agenda_fired_impl/mark_clues_revealed_impl
    同一套"未知 id 拒绝写入、上报为 issue"原则一致。

    P5.2 起同时写两处：房间级指针 + 逐人位置。

    ## 🔴 谁跟着走：**发言者 + 此刻与他同处一地的人**

    真人实测 2026-07-31（exec/19 #37）打脸过一次：最初只挪"本轮发言的人"，
    理由是"分头时留在别处的人不该被隔空传送走"。顾虑本身对，**默认方向选反了**
    ——绝大多数时间全队是在一起的，于是：

        第 1 轮 张家豪发言 → 张家豪@门外（凌铭辉无条目、回落房间指针，同组）
        第 2 轮 凌铭辉发言 → 凌铭辉@门口、房间指针也变门口
                            但张家豪有**显式旧条目**「门外」，不再回落 → 分头！

    两个人明明肩并肩站着，系统却判成分头：叙事分段投递、张家豪什么都收不到，
    连裁决器都读着错误的「各自所在」写下"张家豪在房子外面，未直接参与"，于是
    只给凌铭辉发了检定。一个位置默认值把三件事一起弄坏了。

    改成"跟你站在一起的人跟你一起走"：完全用现有数据算得出来，不需要模型额外
    表达，而且两头都退化正确——全队同处时全员同行；真分头时（`moves` 挪走的那
    位）不在发言者所处的地点，自然不动。
    """
    node = deps.module.node_by_id(node_id)
    if node is None:
        raise KeeperToolError(f"剧本里没有场景节点 id={node_id}")
    speakers = set(deps.turn_player_ids or (deps.player_id,))
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        # AI 玩家算进名单（exec/21 第一层）："跟你站在一起的人跟你一起走"。
        # 不算它，AI 会被永久留在原地，下一轮就被判成分头——正是 #37 那类 bug。
        roster = list(
            (await db.execute(select(Player.id).where(Player.room_id == deps.room_id))).scalars()
        )
        # 用改动**之前**的状态判断"谁跟发言者站在一起"——先写指针再判断会把
        # 所有回落到房间指针的人都算成同处，等于没判。
        speaker_places = {location_of(current_state, pid) for pid in speakers}
        movers = speakers | {
            pid for pid in roster if location_of(current_state, pid) in speaker_places
        }
        # ⚠️ 谁"真的换了地方"也必须在改指针**之前**算——写完 CURRENT_NODE_KEY
        # 再问，所有回落到房间指针的人都会显示成"已经在新节点"，等于没判。
        # （跟上面 speaker_places 同一个坑，写这段时又踩了一次。）
        moved_away = {pid for pid in movers if location_of(current_state, pid) != node_id}

        current_state[CURRENT_NODE_KEY] = node_id
        locations = load_player_locations(current_state)
        for pid in movers:
            locations[pid] = node_id
        current_state[PLAYER_LOCATION_KEY] = serialize_player_locations(locations)
        _drop_stealth_on_move(current_state, moved_away)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.node", {"node_id": node_id, "title": node.title})
    return f"当前场景节点：{node.title}（{node_id}）"


async def clear_current_node_impl(deps: KeeperDeps) -> str:
    """清空场景节点指针：人在剧本节点之外的地方（exec/19 #48）。

    房间级指针与**所有**逐人条目一起清——留着任何一条，那个人的护栏就还挂在
    旧节点上。清空后 location_of 返回 None，护栏退化到即兴层放行
    （filter_checks_against_module 找不到节点就全部放行），这是正确行为：
    剧本没写到的地方本来就没有"模组标注的检定点"可言。
    """
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        if CURRENT_NODE_KEY not in current_state and not load_player_locations(current_state):
            # 本来就没指针 = 无事发生。返回空串让调用方跳过——执行报告是
            # 喂给叙事阶段的"本轮发生了什么"，塞一条无操作进去等于噪声。
            return ""
        current_state.pop(CURRENT_NODE_KEY, None)
        current_state.pop(PLAYER_LOCATION_KEY, None)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.node", {"node_id": None, "title": None})
    return "场景已离开剧本节点范围，节点指针清空"


async def move_player_impl(deps: KeeperDeps, player_name: str, node_id: str) -> str:
    """把**一名**调查员单独挪到某个剧本节点（分头探索，P5.2）。

    与 `set_current_node_impl` 的分工是"默认 vs 覆盖"：那个写「本轮发言的人
    共同到了哪」，这个写「谁没跟着大家、单独在哪」，写的是同一张逐人表。
    一次只处理一个人，是为了让"节点 id 不存在 / 找不到这个玩家"这类问题
    退化成**这一条**的 issue，而不是整批移动一起失败。
    """
    node = deps.module.node_by_id(node_id)
    if node is None:
        raise KeeperToolError(f"剧本里没有场景节点 id={node_id}")
    async with deps.write_lock, deps.session_factory() as db:
        player, _character = await resolve_character(db, deps, player_name)
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        locations = load_player_locations(current_state)
        moved_away = {player.id} if location_of(current_state, player.id) != node_id else set()
        locations[player.id] = node_id
        current_state[PLAYER_LOCATION_KEY] = serialize_player_locations(locations)
        # 离开原地点 → 解除隐匿（exec/19 #46），与 set_current_node_impl 同口径
        _drop_stealth_on_move(current_state, moved_away)
        room.keeper_state = current_state
        await record_event(
            db,
            deps,
            "keeper.move",
            {"player": player.nickname, "node_id": node_id, "title": node.title},
        )
    return f"{player.nickname}单独前往：{node.title}（{node_id}）"


async def set_stealth_impl(deps: KeeperDeps, player_name: str, hidden: bool) -> str:
    """把一名调查员置入 / 移出隐匿状态（exec/18 ②「在场但不可见」）。

    隐匿只影响**他自己的行动被谁看见**；他照常收得到所在地点的叙事——
    "自己听得见"是这条规则的一半，另一半靠 per-observer 投递实现。
    """
    async with deps.write_lock, deps.session_factory() as db:
        player, _character = await resolve_character(db, deps, player_name)
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        hidden_ids = load_hidden_players(current_state)
        if hidden:
            hidden_ids.add(player.id)
        else:
            hidden_ids.discard(player.id)
        current_state[HIDDEN_PLAYERS_KEY] = serialize_hidden_players(hidden_ids)
        room.keeper_state = current_state
        await record_event(
            db, deps, "keeper.stealth", {"player": player.nickname, "hidden": hidden}
        )
    return f"{player.nickname}{'进入隐匿' if hidden else '不再隐匿'}"
