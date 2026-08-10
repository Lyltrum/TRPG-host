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

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.models.event import Event
from app.models.room import Player, Room

logger = structlog.get_logger()

PLAYER_LOCATION_KEY = "玩家位置"

#: 潜行/躲藏中的调查员（exec/18 ②）。「在场但不可见」——他照常**听得见**这里
#: 发生的一切（所以不把他从位置分组里摘出去），但他自己的行动不会广播给同处
#: 的其他人。存 player_id 的逗号串。
HIDDEN_PLAYERS_KEY = "隐匿玩家"

#: 走到了别人所在的地点、但**还没被本人确认**是不是真的会合了（exec/33 §5）。
#: 存 `player_id@node_id`，与 `PLAYER_LOCATION_KEY` 同一套编解码。
#:
#: ## 🔴 为什么需要它：分组此前是概率性的
#:
#: 「谁跟谁在一处」由裁决器**每轮重写**的位置派生，于是**每轮都有一次写错分组的
#: 机会**。2026-08-10 多人实测实证：它把 `current_node_id` 与 `moves` 写矛盾，
#: 被明确留下的队友被拖进地下室 → 系统认为没分头 → 全房间广播是**完全正确的
#: 执行**，只是建立在错的位置上。投递层再结构化也没用——**保证等于最弱的那一环**。
#:
#: ## 🔴 协议是不对称的，因为两个方向的错误代价不同
#:
#: - **分开**判错 → 多隔离了一个人：困惑、可恢复、**不泄露** → 乐观执行，不打断。
#: - **会合**判错 → 两组信息当场合并：**泄露、不可撤回** → 必须由**当事人**确认。
#:
#: 这是「受众算错必须朝保密方向失败，绝不退化成广播」的直接应用。确认之所以
#: 合法，是因为问的是当事人自己知道的事（"你走回客厅跟大家会合了吗"）——
#: 跟被否掉的「房主确认结局」正相反，那次是问一个按设计就不该有信息的人。
#:
#: **位置照常写**（它仍是唯一地基，不新增第二份真相）；这个键只让 `group_players`
#: 在**投递侧**保守一点：没确认之前，这个人自己一组。
PENDING_MERGE_KEY = "待确认会合"

#: 这一局即兴出来的地点（exec/32）。玩家去了剧本图外的地方——「卡比家」原文
#: 提过但没建成节点——此前位置这块地基对它失效：`exec/31 #72` 修掉了"保留旧值
#: 说谎"，但人到底在哪仍然没有答案，多人分头时两个人跑去两个不同的图外地方
#: 还会被判成站在一起。
#:
#: 🔴 **存运行时状态，不往模组里塞节点**：模组是被加载的程序，一局里即兴出来的
#: 地方属于这一局的世界状态，下一局同一份模组不该带着它（见 `[[engine-framing]]`）。
#: 形态是 dict（同 `NPC_STATE_KEY`）：`{"loc-1": {"name": ..., "from": ...}}`。
IMPROVISED_LOCATION_KEY = "即兴地点"

#: 即兴地点 id 的前缀。**id 由代码分配**，模型只能从局面块里挑已有的——
#: 让它自己起 id 就是「不要用自由文本当标识符」的又一次复发（「卡比家」
#: 「卡比的家」「卡普顿宅」会变成三个地点）。
IMPROVISED_ID_PREFIX = "loc-"

#: 超过这个条数就打一条 warning。**膨胀本身是信号，不是要治的病**：它说明模型
#: 在拿地点表当便签本，那时该查的是"它缺哪一类落点"，而不是给这张表加裁剪
#: （局面块必须全量列出，藏起来的地点模型看不见，就会重建一个同名的）。
IMPROVISED_SOFT_LIMIT = 8


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


def load_improvised_locations(keeper_state: dict | None) -> dict[str, dict]:
    """解析即兴地点表。形状不对的条目整条丢弃，不产生半条记录。"""
    if not keeper_state:
        return {}
    raw = keeper_state.get(IMPROVISED_LOCATION_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for loc_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        name = str(payload.get("name") or "").strip()
        if not loc_id or not name:
            continue
        out[str(loc_id)] = {"name": name, "from": payload.get("from") or None}
    return out


def next_improvised_id(table: dict[str, dict]) -> str:
    """下一个可用 id。**只增不复用**——复用会让复盘里两个地方共用一个 id。"""
    used = 0
    for loc_id in table:
        if loc_id.startswith(IMPROVISED_ID_PREFIX):
            suffix = loc_id[len(IMPROVISED_ID_PREFIX) :]
            if suffix.isdigit():
                used = max(used, int(suffix))
    return f"{IMPROVISED_ID_PREFIX}{used + 1}"


def resolve_location(
    module: ScenarioModule, keeper_state: dict | None, location_id: str | None
) -> str | None:
    """一个位置 id 叫什么名字。剧本节点 → 标题；即兴地点 → 它的名字；都不是 → None。

    🔴 抽出来是因为这件事原本散在 6 处，每处都写着
    `module.node_by_id(x) ... or "此处"`——加一类位置就要挨个改，漏一处就渲染成
    「此处」。判据同 `exec/27`：**同一件事的两头，一头可插拔一头写死**是最坏的信号。
    """
    if not location_id:
        return None
    node = module.node_by_id(location_id)
    if node is not None:
        return node.title
    improvised = load_improvised_locations(keeper_state).get(location_id)
    if improvised is not None:
        return improvised["name"]
    return None


def location_of(keeper_state: dict | None, player_id: str) -> str | None:
    """这名调查员现在在哪个剧本节点。逐人表优先，回落房间级指针（见模块 docstring）。"""
    located = load_player_locations(keeper_state).get(player_id)
    if located:
        return located
    return load_current_node_id(keeper_state)


def load_pending_merges(keeper_state: dict | None) -> dict[str, str]:
    """待确认会合：player_id → 他走到的那个地点 id。编解码同逐人位置表。"""
    if not keeper_state:
        return {}
    raw = keeper_state.get(PENDING_MERGE_KEY)
    if raw is None or raw == "":
        return {}
    out: dict[str, str] = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part or "@" not in part:
            continue
        player_id, node_id = part.split("@", 1)
        if player_id.strip() and node_id.strip():
            out[player_id.strip()] = node_id.strip()
    return out


def group_players(
    keeper_state: dict | None, player_ids: list[str]
) -> list[tuple[str | None, list[str]]]:
    """把一组玩家按位置分组，返回 [(node_id, [player_id, ...]), ...]。

    保 `player_ids` 的入参顺序（分组顺序 = 各组第一个人出现的顺序），叙事
    分段的顺序因此是确定的、可断言的。位置未知（None）自成一组——**不要**
    把它们并进任何已知位置的组，"不知道他在哪"不等于"他在这儿"。

    🔴 **待确认会合的人单独成组**（exec/33 §5）：他的位置已经写进去了，但"是不是
    真的跟那边的人碰上了"还没被他本人确认。在确认之前按**没碰上**处理——
    这个方向判错只是多隔离一个人，反过来判错就是不可撤回的泄露。
    """
    pending = load_pending_merges(keeper_state)
    out: list[tuple[str | None, list[str]]] = []
    index: dict[str | None, int] = {}
    for pid in player_ids:
        location = location_of(keeper_state, pid)
        if pid in pending:
            out.append((location, [pid]))  # 自己一组，且**不进 index**，别人并不进来
            continue
        if location in index:
            out[index[location]][1].append(pid)
        else:
            index[location] = len(out)
            out.append((location, [pid]))
    return out


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
        title = resolve_location(module, keeper_state, node_id)
        where = f"{title}（{node_id}）" if title is not None else (node_id or "（位置未记录）")
        who = "、".join(
            f"{nicknames.get(pid, pid)}（隐匿中）" if pid in hidden else nicknames.get(pid, pid)
            for pid in members
        )
        lines.append(f"- {where}：{who}")
    return "\n".join(lines)


async def snapshot_locations(deps: KeeperDeps) -> dict[str, str | None]:
    """回合开始时每个人在哪。会合检测要用它当基线（exec/33 §5）。"""
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        state = room.keeper_state if room is not None else None
        roster = list(
            (await db.execute(select(Player.id).where(Player.room_id == deps.room_id))).scalars()
        )
    return {pid: location_of(state, pid) for pid in roster}


async def record_merges_since(deps: KeeperDeps, before: dict[str, str | None]) -> list[str]:
    """跟**回合开始时不在一处**的人变成了同处 → 给真人挂上待确认会合。

    🔴 **必须按回合前后比对，不能在每次写入时各判各的**（2026-08-10 验证跑当场
    抓到）：`current_node_id` 把一批人挪过去、紧接着 `moves` 又逐个写一遍时，
    后写的那个人会看见"目的地已经有人"——那是**同一批一起走的队友**，被判成了
    会合，凭空弹一张确认卡。会合的定义是"**之前不在一起，现在在一起**"，
    只有回合级的前后快照答得了这个问题。

    AI 队友不挂卡：它没有独立意图，跟着谁走由代码定（`exec/21` 第一层）。
    """
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return []
        current_state = dict(room.keeper_state or {})
        humans = set(
            (
                await db.execute(
                    select(Player.id).where(Player.room_id == deps.room_id, Player.is_ai.is_(False))
                )
            ).scalars()
        )
        after = {pid: location_of(current_state, pid) for pid in before}
        pending = load_pending_merges(current_state)
        asked: list[str] = []
        for pid in sorted(humans & set(before)):
            here = after.get(pid)
            if here is None or pid in pending:
                continue
            # 🔴 只问**这一轮动了的人**：他才是做了决定的那个，知道自己有没有
            # 真的走过去。站着没动的人被别人走过来，不该额外弹一张卡——一次会合
            # 弹两张，玩家会以为出了两件事。
            if before.get(pid) == here:
                continue
            newly_together = [
                other
                for other, other_here in after.items()
                if other != pid and other_here == here and before.get(other) != before.get(pid)
            ]
            if newly_together:
                pending[pid] = here
                asked.append(pid)
        if not asked:
            return []
        current_state[PENDING_MERGE_KEY] = serialize_player_locations(pending)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.merge_pending", {"players": asked})
    return asked


def _drop_stale_pending_merge(state: dict, player_ids: set[str], node_id: str | None) -> None:
    """人又走了（去了别处、或位置被清空）→ 那张确认卡作废。就地改 `state`。

    不清的话他会永远单独一组：**没确认就维持分离**是对的默认，但**已经离开
    那个地点之后还挂着**就变成了永久隔离，那是另一个 bug。
    """
    pending = load_pending_merges(state)
    stale = [pid for pid in player_ids if pid in pending and pending[pid] != node_id]
    if not stale:
        return
    for pid in stale:
        pending.pop(pid, None)
    if pending:
        state[PENDING_MERGE_KEY] = serialize_player_locations(pending)
    else:
        state.pop(PENDING_MERGE_KEY, None)


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
    speakers = set(deps.turn_player_ids or (deps.player_id,))
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        # 白名单 = 剧本节点 ∪ 本局的即兴地点（exec/32）。校验挪进事务里，是因为
        # 即兴地点存在 keeper_state 里，不读库就不知道有没有这个 id——
        # **白名单不因为多了一类位置而变松**，两边都查不到仍然拒绝。
        title = resolve_location(deps.module, current_state, node_id)
        if title is None:
            raise KeeperToolError(f"没有 id={node_id} 的地点（剧本节点与即兴地点里都找不到）")
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
        _drop_stale_pending_merge(current_state, movers, node_id)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.node", {"node_id": node_id, "title": title})
    return f"当前场景节点：{title}（{node_id}）"


async def create_improvised_location_impl(
    deps: KeeperDeps, name: str, from_id: str | None = None
) -> tuple[str, str]:
    """在这一局的地点表里建一个剧本没有的地方，返回 (地点 id, 可读报告)。

    **只建表，不挪人**——挪人仍然走 `set_current_node_impl`，那样"谁跟着走"
    「离开就解除隐匿」这些语义只有一份实现。调用顺序在 `movement/executor.py`。

    🔴 **重名不去重**：两个「墓地」是两条。名字是给人看的，不是标识符——
    去重就等于拿自由文本当 key，那正是这套设计要避免的（`exec/17`）。
    """
    cleaned = name.strip()
    if not cleaned:
        raise KeeperToolError("新地点必须有名字")
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        table = load_improvised_locations(current_state)
        # 来路只留能解析的：解析不出就丢掉这一项，**不把模型写的字符串原样存下来**
        # （存了它就会被当成 id 用，又一次自由文本当标识符）。
        origin = from_id if resolve_location(deps.module, current_state, from_id) else None
        loc_id = next_improvised_id(table)
        table[loc_id] = {"name": cleaned, "from": origin}
        current_state[IMPROVISED_LOCATION_KEY] = table
        room.keeper_state = current_state
        await record_event(
            db, deps, "keeper.location", {"location_id": loc_id, "name": cleaned, "from": origin}
        )
        if len(table) > IMPROVISED_SOFT_LIMIT:
            # 观测，不是限流：条数失控说明模型在拿地点表当便签本，那时该查的是
            # "它缺哪一类落点"（exec/31 §Ⅰ），不是给这张表加裁剪。
            logger.warning(
                "keeper_improvised_locations_growing",
                room_id=deps.room_id,
                count=len(table),
                limit=IMPROVISED_SOFT_LIMIT,
            )
    return loc_id, f"新地点：{cleaned}（{loc_id}）"


async def clear_current_node_impl(deps: KeeperDeps) -> str:
    """本轮发言的人走到了剧本节点之外，承认不知道他们在哪（exec/19 #48）。

    清空后 `location_of` 返回 None，护栏退化到即兴层放行
    （`filter_checks_against_module` 找不到节点就全部放行），这是正确行为：
    剧本没写到的地方本来就没有"模组标注的检定点"可言。

    ## 🔴 只清**这些人**的位置，不是所有人的（2026-08-10 多人实测实锤）

    第一版把整张逐人表 `pop` 掉了。多人实测的证据链：阿贵一个人说「我离开客厅
    去门厅」（门厅不是剧本节点）→ 清空 → **在地下室的阿福也丢了位置** →
    `group_players` 判成"全都不知道在哪" = 同一组 = **不再算分头** →
    下一段本该只发给阿福的结算叙事**广播给了全房间**（事件表里 audience 为空）。

    一个人走出地图，抹掉的却是所有人的位置——**分头状态被一次清空推平**，
    而私密性正是从位置派生的（`exec/18`：你不在场，所以你不知道）。
    与 `set_current_node_impl` 同一套口径：**动的是发言者和此刻与他同处的人**。
    """
    speakers = set(deps.turn_player_ids or (deps.player_id,))
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        locations = load_player_locations(current_state)
        if CURRENT_NODE_KEY not in current_state and not locations:
            # 本来就没指针 = 无事发生。返回空串让调用方跳过——执行报告是
            # 喂给叙事阶段的"本轮发生了什么"，塞一条无操作进去等于噪声。
            return ""
        roster = list(
            (await db.execute(select(Player.id).where(Player.room_id == deps.room_id))).scalars()
        )
        speaker_places = {location_of(current_state, pid) for pid in speakers}
        leaving = speakers | {
            pid for pid in roster if location_of(current_state, pid) in speaker_places
        }
        # 房间指针是"没被单独定位过的人在哪"的默认值，跟着走的人正是 leaving，
        # 所以它一起清。**留在别处的人有显式条目，不受影响。**
        current_state.pop(CURRENT_NODE_KEY, None)
        for pid in leaving:
            locations.pop(pid, None)
        if locations:
            current_state[PLAYER_LOCATION_KEY] = serialize_player_locations(locations)
        else:
            current_state.pop(PLAYER_LOCATION_KEY, None)
        # 离开原地点 → 解除隐匿（exec/19 #46）。清空也是"离开"，第一版漏了这一半：
        # 实测里阿贵藏在窗帘后、说"我离开客厅去门厅"，之后 `隐匿玩家` 还挂着他。
        _drop_stealth_on_move(current_state, leaving)
        # 人已经不在那个地点了，那张会合确认卡作废（exec/33 §5.3）
        _drop_stale_pending_merge(current_state, leaving, None)
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
    async with deps.write_lock, deps.session_factory() as db:
        player, _character = await resolve_character(db, deps, player_name)
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        title = resolve_location(deps.module, current_state, node_id)
        if title is None:
            raise KeeperToolError(f"没有 id={node_id} 的地点（剧本节点与即兴地点里都找不到）")
        locations = load_player_locations(current_state)
        moved_away = {player.id} if location_of(current_state, player.id) != node_id else set()
        locations[player.id] = node_id
        current_state[PLAYER_LOCATION_KEY] = serialize_player_locations(locations)
        # 离开原地点 → 解除隐匿（exec/19 #46），与 set_current_node_impl 同口径
        _drop_stealth_on_move(current_state, moved_away)
        _drop_stale_pending_merge(current_state, {player.id}, node_id)
        room.keeper_state = current_state
        await record_event(
            db,
            deps,
            "keeper.move",
            {"player": player.nickname, "node_id": node_id, "title": title},
        )
    return f"{player.nickname}单独前往：{title}（{node_id}）"


async def only_speakers_named(deps: KeeperDeps, player_names: list[str]) -> bool:
    """`moves` 点名的这些人，是不是**全都是本轮发言的人**（exec/33 验证跑）。

    区分模型两种写法的唯一依据是**谁被点名**：

    - 「我一个人去 X」→ `node=X` + `moves=[发言者→X]`，点的是自己；
    - 「全队去 X」→ `node=X` + `moves=[其他每个人→X]`，**发言者不在里面**。

    只看"目标节点是否相同"分不开这两种，第一版就是那么写的——结果第二种写法
    被判成"只有他去"，`current_node_id` 被丢掉，**发言者反而被留在原地**。

    解析不出来的名字（模型编的人名）当成"不是发言者"：那时不该丢掉
    `current_node_id`，**保守方向是照常移动大家**，而不是让发言者悬空。
    """
    speakers = set(deps.turn_player_ids or (deps.player_id,))
    if not player_names:
        return False
    async with deps.session_factory() as db:
        for name in player_names:
            try:
                player, _character = await resolve_character(db, deps, name)
            except KeeperToolError:
                return False
            if player.id not in speakers:
                return False
    return True


async def confirm_merge_impl(db: AsyncSession, room_id: str, player_id: str) -> bool:
    """当事人确认「我确实跟他们碰上了」——从此按同一组投递。返回是否真的有变化。

    **只有一个动作，没有"否认"**：不确认就是维持分离，那本来就是默认与安全方向
    （`exec/33 §5.3`）。🔴 也**没有超时自动确认**——超时自动 = 静默泄露。

    不走 `KeeperDeps`：这条路径**不碰剧本、不掷骰、不裁决**，只是把一条待确认
    记录去掉。串行由调用方拿房间行动锁保证（keeper_state 是整列读-改-写）。
    """
    room = await db.get(Room, room_id)
    if room is None:
        raise KeeperToolError("房间不存在")
    current_state = dict(room.keeper_state or {})
    pending = load_pending_merges(current_state)
    if player_id not in pending:
        return False
    node_id = pending.pop(player_id)
    if pending:
        current_state[PENDING_MERGE_KEY] = serialize_player_locations(pending)
    else:
        current_state.pop(PENDING_MERGE_KEY, None)
    room.keeper_state = current_state
    db.add(
        Event(
            room_id=room_id,
            player_id=player_id,
            event_type="keeper.merge_confirmed",
            payload={"node_id": node_id},
        )
    )
    await db.commit()
    return True


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
