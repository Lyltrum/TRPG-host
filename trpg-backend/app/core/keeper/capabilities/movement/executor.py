"""movement 能力的执行层：场景指针 → 分头移动 → 隐匿，顺序有语义。"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.keeper.capabilities.movement.schema import PlayerMove as _Move
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError, resolve_character
from app.core.keeper.runtime.location_state import (
    IMPROVISED_ID_PREFIX,
    clear_current_node_impl,
    create_improvised_location_impl,
    move_player_impl,
    only_speakers_named,
    record_merges_since,
    resolve_location,
    set_current_node_impl,
    set_stealth_impl,
    snapshot_locations,
)
from app.core.keeper.runtime.scene_state import load_current_node_id
from app.models.room import Room

logger = structlog.get_logger()


def _position_left_the_map(attempted_node_id: str | None, facts: TurnFacts) -> bool:
    """这一轮「人在哪」没能落在任何剧本节点上。

    两种情况语义上是同一件事，都必须走清空：

    - **主路没走**：裁决器换了场景却给不出节点 id（exec/19 #48）。只在它
      **明确写了新的「当前场景」**时才成立——没提场景的普通轮次（对话、检定
      结算）不该动节点指针。
    - **主路失败**：给了 id 但那个 id 不是剧本节点（exec/31 #72，真机三次全中：
      玩家说「去卡比家」，裁决器写了一个 **NPC id**）。它写下这个 id 本身就是在
      说"人已经不在原处了"，所以不必再等场景声明。

    🔴 原来这两支写成 `if / elif`：主路抛异常被 except 吞成 issue 之后，**兜底
    永远轮不到**，指针保留旧值 = 静默说谎（护栏拿错节点的检定表卡玩家、分组也
    跟着错）。判据：**兜底的触发条件要包含「主路失败」，不能只包含「主路没走」。**

    「有没有声明新场景」由 `world_state` publish 进 `TurnFacts`（那个字段是它
    的），本能力只 consume。切分之初这里直接读 `decision.state_updates`——一片
    能力伸手进另一片的字段，没有 import 所以架构测试抓不到，正是最坏的那种
    **隐式**耦合。
    """
    if attempted_node_id:
        return True
    return facts.scene_name_declared is not None


async def _repoint_verdict(deps: KeeperDeps, node_id: str, facts: TurnFacts) -> str | None:
    """本轮自己说了「场景没变」，却要把指针挪到**别的**节点——自相矛盾。

    返回 `"block"`（拦下）/ `"warn"`（记 issue 但照常执行）/ `None`（没问题）。

    ## 🔴 实据（2026-08-14 真人实测）

    玩家在温特公寓连查四轮（翻抽屉、查通讯录、看档案夹），每一轮裁决都写
    `当前场景=温特公寓`（**原样重写，值没变**）**同时**把 `current_node_id`
    改成 `investigation-start`。那个 id 是存在的（所以既有的存在性校验拦不住），
    标题叫「调查起点」，在任何调查场景下模型都觉得它像对的。

    下游后果不是"显示错了"：护栏按节点取 `checks[]`，日志里因此出现 4 次
    「当前场景『地窖』模组标注检定点为 `['dodge']`，不允许即兴 spot-hidden」
    ——门在按错误的节点执行。十次错误全是这个形状。

    ## 🔴 为什么分成「拦」和「只报」两档

    「场景名没变却改节点」并**不总是**错的：两个不同节点可以有同一个显示名
    （`test_node_id_change_injects_even_when_scene_text_matches` 守着这条——
    玩家移动到另一处也叫这个名字的地方，那时该注入过渡拍）。语义上区分不开
    "错误改写"和"移动到同名地点"，硬拦就会误伤真实需求。

    但有一个**完全确定**的子集：**人当前站在即兴地点（`loc-N`）上**。
    `loc-N` 的语义就是"剧本里没有这个地方"——既然场景没变、人还站在剧本外，
    那么把指针挪到任何剧本节点都是错的，不存在"同名的另一处"这种解释。
    实测十次里有八次正是这一种（`loc-5` 温特公寓 → `investigation-start`）。

    ## 🔴 2026-08-15：第二个确定子集——**两端标题不同**

    上面那句"语义上区分不开"当时是对的，但它把免死金牌发得太宽了。回归实测
    又抓到两次，全是剧本节点之间：

        场景「度假屋卧室」原样重写
            + `bedroom-one`（卧室一）→ `master-bedroom`（主卧）
        场景「度假屋外的森林」原样重写
            + `forest-wandering`（森林漫步）→ `cabin-exterior`（度假屋外观）

    两次的**两端标题都不一样**。而"移动到另一处也叫这个名字的地方"这条豁免，
    前提正是**两处同名**——标题不同时它根本不成立，不存在需要保护的真实需求。
    于是判据收成：**场景没变 + 两端标题不同 ⇒ 拦**；标题相同才留 warn
    （那时确实分不清，仍按"报而不断"处理，`test_node_id_change_injects_even_
    when_scene_text_matches` 守的就是这一支）。

    🔴 **取不到标题就不拦**（`resolve_location` 返回 None）：那是缺数据，不是
    证据。显式降级成 warn，不拿"查不到"当"不一样"用。

    ## 🔴 为什么这条值得从 warn 升成 block

    指针不是只影响显示。回归实测量出来它是**四条症状的同一个根因**：护栏按
    玩家所在节点取 `checks[]`（指针错 ⇒ 查错白名单 ⇒ 检定被静默吞掉）、
    `format_san_points` 按玩家所在节点注入理智检定点（指针错 ⇒ 整块不渲染 ⇒
    模型眼前没有 SAN 提示）、顶栏位置提示直读指针、收尾门按节点数。
    """
    if not facts.scene_name_restated:
        return None
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        keeper_state = room.keeper_state if room is not None else None
        current = load_current_node_id(keeper_state)
    if current is None or current == node_id:
        return None
    if current.startswith(IMPROVISED_ID_PREFIX) and not node_id.startswith(IMPROVISED_ID_PREFIX):
        return "block"
    current_title = resolve_location(deps.module, keeper_state, current)
    target_title = resolve_location(deps.module, keeper_state, node_id)
    if current_title and target_title and current_title != target_title:
        return "block"
    return "warn"


async def _resolve_player_id(deps: KeeperDeps, label: str) -> str | None:
    """昵称/角色名 → 玩家 id。解析不出返回 None（由后面那句照常报 issue）。

    `facts.stealth_check_players` 装的是 id，所以比较之前两边都得落到 id 上
    ——两个自由文本直接比就是同义词打地鼠（exec/17）。
    """
    async with deps.session_factory() as db:
        try:
            player, _character = await resolve_character(db, deps, label)
        except KeeperToolError:
            return None
    return player.id


async def execute_movement(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """四步顺序不能换：

    0. `new_location` 建表（exec/32）——必须排在最前，这一轮建的地点要能立刻
       被当作落点用；建完**就是发言者的落点**，除非裁决器另写了 `current_node_id`。
    1. `current_node_id` 是"本轮发言者的默认落点"；
    2. `moves` 是"谁不跟大家一起"——必须排在默认落点**之后**，否则被盖掉；
    3. `hiding` 与移动同一类空间状态，逐条执行、逐条记 issue。
    """
    report: list[str] = []
    issues: list[str] = []
    # 会合检测的基线：**回合开始时**谁在哪（exec/33 §5）。必须在任何移动之前取，
    # 也必须按回合比对——逐次写入各判各的会把"同一批一起走的队友"判成会合。
    before_locations = await snapshot_locations(deps)

    node_id = getattr(decision, "current_node_id", None)
    new_location = getattr(decision, "new_location", None)
    moves = list(getattr(decision, "moves", ()))
    # 「这一轮的落点已经安排好了」——下面那条「走出剧本图 → 清空」的兜底只在
    # 它为假时才该跑。🔴 它是**逐个列出情况**的地方（现在三种：消解交给 moves /
    # movers 逐人指定 / 房间指针设成功），加一种落点就要回来加一条，否则兜底
    # 会把刚安排好的位置抹掉。`movers` 第一版就漏了，真机当场把留在原地的
    # 队友清成 None、两组并回一组（2026-08-11）。
    landing_handled = False
    if new_location is not None:
        try:
            created_id, line = await create_improvised_location_impl(
                deps, new_location.name, new_location.from_id
            )
            report.append(line)
            if new_location.movers:
                # 🔴 只有一部分人去（望风、绕后、留在门口）：翻译成逐人 `moves`，
                # **不动房间指针**，其他人留在原地。id 是代码刚分配的，模型写不出来，
                # 所以这条"引用本轮新建地点"的通路只能由代码接上。
                moves = moves + [
                    _Move(player=name, node_id=created_id) for name in new_location.movers
                ]
                landing_handled = True
            else:
                # 建了却没说谁去 = 全队一起去。裁决器显式写了别的落点时不覆盖它
                # （它可能是"我打发 NPC 去卡比家"这种，人并没有过去）。
                node_id = node_id or created_id
        except KeeperToolError as exc:
            issues.append(f"新地点未建立：{exc}")

    named_here = [move for move in moves if move.node_id == node_id] if node_id else []
    if named_here and await only_speakers_named(deps, [m.player for m in named_here]):
        # 🔴 矛盾信号消解（2026-08-10 多人实测）：`moves` 把**发言者本人**单独挪到了
        # `current_node_id` 指的那个地方——那就是"只有他去"的意思。
        #
        # 实测原话「我去地下室看看，阿贵你留在客厅」，裁决器写了
        # `current_node_id=basement-laboratory` **且** `moves=[阿福→basement-laboratory]`
        # （thinking 写着"处理分头"）。而 `current_node_id` 会带上"此刻与发言者同处
        # 的人"（`exec/19 #37` 的默认值），于是被明确留下的阿贵**也被拖进了地下室**：
        # 叙事说他在客厅、结构化位置说他在地窖，两边各说各话。
        #
        # 🔴 **必须看"点名的是谁"，不能只看"目标节点相同"**——第一版就是只看目标，
        # 当天的验证跑里当场反噬：模型表达「全队一起去」的写法是
        # `node=X + moves=[其他每个人→X]`（发言者不在 moves 里），被判成"只有他去"，
        # **发言者反而被留在原地**，位置成了 None。
        # 两种写法的区别只在**谁被点名**：点自己 = 我一个人去；点别人 = 带上他们。
        report.append(f"场景定位交给 moves 执行（{node_id} 已被逐人指定）")
        node_id = None
        handed_to_moves = True
        landing_handled = True
    else:
        handed_to_moves = False

    located = landing_handled
    if node_id and (verdict := await _repoint_verdict(deps, node_id, facts)) is not None:
        # 自相矛盾：这一轮明写了「当前场景」且值没变（= 人还在原地），却要把
        # 节点指针挪到别处。两档处理的理由与实据见 `_repoint_verdict`。
        #
        # 🔴 这道门配着一条走得通的修法：真的移动了，就把「当前场景」也改掉
        # ——那本来就是规则 4 要求的（「玩家移动后**必须**更新『当前场景』
        # **并且**把 current_node_id 设为对应 id」）。
        issues.append(
            f"场景定位{'未执行' if verdict == 'block' else '可疑'}："
            f"本轮写的「当前场景」跟上一轮相同（人还在原地），"
            f"不该同时把场景节点改成 {node_id!r}"
        )
        if verdict == "block":
            node_id = None
        else:
            logger.warning(
                "keeper_repoint_while_scene_unchanged", room_id=deps.room_id, node_id=node_id
            )
    if node_id:
        # node_id 存在性由 set_current_node_impl 校验（module.node_by_id）——
        # 非法 id 不写入、记为 issue，不炸整轮。
        try:
            report.append(await set_current_node_impl(deps, node_id))
            located = True
        except KeeperToolError as exc:
            issues.append(f"场景定位未执行：{exc}")
    if not located and _position_left_the_map(node_id, facts):
        # 🔴 `located` 把"消解掉的那一支"也算进来（2026-08-10 验证跑）：消解的
        # 意思是**这一轮的落点由 moves 逐人负责**，不是"大家都走出了剧本图"。
        # 漏算的后果是它紧接着落进下面这条清空——把明确留在原地的队友一并抹掉。
        #
        # 人在剧本节点之外：换了场景却没有节点对应得上（exec/19 #48），
        # 或者给出的 id 根本不是节点（exec/31 #72）。判据见上面那个谓词。
        #
        # 试玩实测：终局「当前场景 = 科比特家门外（警察到场）」，而节点指针还
        # 停在 basement-laboratory——玩家已经站在屋外，护栏却拿地下室的 checks[]
        # 去卡他的检定。裁决器**做对了**（找不到对应节点就留空，不编造 id），
        # 错在代码把"没说"当成了"没变"。
        #
        # 正确语义是**清空**：人在剧本节点之外的地方，护栏退化到即兴层放行。
        # 与 #37 同族——空间状态是地基，宁可承认不知道，不可拿旧值硬撑。
        try:
            cleared = await clear_current_node_impl(deps)
            if cleared:
                report.append(cleared)
        except KeeperToolError as exc:
            issues.append(f"场景指针清空未执行：{exc}")

    # 🔴 被**逐人点名**挪动的人（`moves` / `new_location.movers` 都归到这里）。
    # 会合确认要靠它区分「他自己说要过去」和「他被推断过去」——见
    # `record_merges_since` 的 `self_declared`。
    named_movers: set[str] = set()
    for move in moves:
        try:
            moved_id, line = await move_player_impl(deps, move.player, move.node_id)
            named_movers.add(moved_id)
            report.append(line)
        except KeeperToolError as exc:
            issues.append(f"分头移动未执行：{exc}")

    for change in getattr(decision, "hiding", ()):
        try:
            # 🔴 **本轮要掷潜行的人，「进入隐匿」不在这里生效**（2026-08-15）。
            #
            # 回归实测抓到的形态：裁决同时写了 `checks:[stealth]` 和
            # `hiding:[{hidden:true}]`，检定被护栏吞掉，而隐匿状态照样落库
            # ——**藏起来是白给的**，两次都是，第二次是贴到三步外的怪物旁边。
            # 病根不是护栏，是这两条路互不相干：掷不掷、成不成功，对"他藏没
            # 藏住"没有任何影响。
            #
            # 改后由 `apply_skill_check` 按掷出来的结果决定进不进隐匿，这里
            # 只负责**不要抢先替它下结论**。
            #
            # 🔴 只拦 `hidden=true`：现身/被发现是无条件的，不需要谁同意，也
            # 不该因为"本轮碰巧掷了个潜行"就延后。同族于 `open_threads`
            # 那条「进入由代码定，结束必须走 schema 字段」的不对称。
            #
            # 🔴 没有检定的 `hidden=true` 仍然立刻生效：没人看得见的时候躲起来
            # 本来就不必掷（真人桌同理），那是 KP 有权直接给的。
            if change.hidden:
                hider = await _resolve_player_id(deps, change.player)
                if hider is not None and hider in facts.stealth_check_players:
                    report.append(f"{change.player} 是否藏住，等这一轮的潜行检定结果")
                    continue
            report.append(await set_stealth_impl(deps, change.player, change.hidden))
        except KeeperToolError as exc:
            issues.append(f"潜行状态未执行：{exc}")

    # 谁跟"回合开始时不在一处的人"碰上了 → 挂起，等他本人确认（exec/33 §5.2）。
    # 分开是安全方向、乐观执行；会合是危险方向、必须有人点头。
    if await record_merges_since(deps, before_locations, self_declared=named_movers):
        report.append("有人走到了别人所在的地方，等本人确认是否会合")

    # 🔴 保险丝：消解分支说明裁决器**点名让发言者单独去某处**（= 它想分头），
    # 而全队仍在同一个位置 → 分头没成立。这**不是闸门**（零命中不代表没问题），
    # 它存在的唯一理由是：2026-08-10 两次真机分头失败，系统里没有任何东西会说
    # 一声，上一跑还因为"清空位置"的副作用给出了**假的成功**。
    #
    # 触发条件只能是消解分支，不能是"写了 moves"：`node=X + moves=[其他每个人→X]`
    # 是「全队一起去」的合法写法，那时大家在同一处本来就是对的。
    if handed_to_moves:
        await _report_split_that_did_not_take(deps, issues)

    return report, issues


async def _report_split_that_did_not_take(deps: KeeperDeps, issues: list[str]) -> None:
    """点名单独行动之后大家仍在一处 → 记一条 issue，让叙事和日志都看得见。"""
    after = await snapshot_locations(deps)
    if len(after) > 1 and len(set(after.values())) == 1:
        issues.append(
            "分头未成立：裁决点名让人单独行动，但全队仍在同一个位置——要让一部分人"
            "待在别处，得给那个地方一个落点（new_location 带 movers）"
        )
        logger.warning(
            "keeper_split_did_not_take",
            room_id=deps.room_id,
            location=next(iter(after.values())),
            players=len(after),
        )
