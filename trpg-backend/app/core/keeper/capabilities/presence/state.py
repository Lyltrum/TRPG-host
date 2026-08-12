"""谁刚到、谁刚走，而守秘人还没交代过。

## 这一片补的是什么

中途加入放开之后（`join_room` 不再拦 InGame），晚到的朋友会**凭空出现在
在场名单里**；中途离开（`Player.away`）则让人**凭空从名单里消失**。两头都
需要守秘人在剧情里圆一句，否则玩家看到的是队友像鬼一样闪现和蒸发。

用户对离开那半的原话：**「让 AI 主持人以合理的方式直接让这个角色暂时消失，
但是不能影响剧情，也要符合逻辑。」** 入场是它的镜像，同一套机制。

## 🔴 存法是「已宣告集合」，不是「待办队列」

第一反应会写成一次性队列（公告塞进去、叙事完清空）。那样**叙事那一拍失败就
丢**，而且重试重跑时公告已经没了。这里改成跟 `已触发理智检定点` 完全同构的
形状：**记「已经交代过谁」，需要交代的人 = 在场名单 减去 已交代集合**。
只有真执行过才记账，重跑天然幂等。

离场那半不能只记 id：人已经不在名单里了，渲染时拿不到他的昵称。所以离场
记的是 `id@昵称`——那是**展示文本**，不是标识符（配对仍然按 id）。
"""

from __future__ import annotations

from app.core.keeper.contract.registry import SituationContext

#: 已经在剧情里交代过登场的玩家 id（逗号串，累积）。
ANNOUNCED_ARRIVALS_KEY = "已交代登场"

#: 还没交代离场的人：`player_id@昵称` 的逗号串。**交代过就删掉**——他不在
#: 在场名单里，没有别的地方能算出这个差集。
PENDING_DEPARTURES_KEY = "待交代离场"


def load_announced_arrivals(keeper_state: dict | None) -> list[str]:
    return _split(keeper_state, ANNOUNCED_ARRIVALS_KEY)


def load_pending_departures(keeper_state: dict | None) -> list[tuple[str, str]]:
    """[(player_id, 昵称)]。形状不对的整条丢弃。"""
    out: list[tuple[str, str]] = []
    for part in _split(keeper_state, PENDING_DEPARTURES_KEY):
        if "@" not in part:
            continue
        player_id, name = part.split("@", 1)
        player_id, name = player_id.strip(), name.strip()
        if player_id and name:
            out.append((player_id, name))
    return out


def serialize_departures(rows: list[tuple[str, str]]) -> str:
    return ", ".join(f"{pid}@{name}" for pid, name in rows)


def _split(keeper_state: dict | None, key: str) -> list[str]:
    if not keeper_state:
        return []
    raw = keeper_state.get(key)
    if raw is None or raw == "":
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def unannounced_arrivals(
    keeper_state: dict | None, players: tuple[tuple[str, str], ...]
) -> list[tuple[str, str]]:
    """在场名单里还没被交代过登场的人。

    ⚠️ **开局第一轮全桌都算"刚到"**，这是有意的：开场那一拍本来就该把在座的人
    介绍一遍（`exec/33 #84` 补的在场名单说的正是这件事）。执行钩子跑完就把他们
    记成已交代，第二轮起只剩真正新来的人。
    """
    announced = set(load_announced_arrivals(keeper_state))
    return [(pid, name) for pid, name in players if pid not in announced]


def format_presence(context: SituationContext) -> str:
    """局面块正文。没人要交代就返回空串——整块不渲染（退化保证）。"""
    arrivals = unannounced_arrivals(context.keeper_state, context.players)
    departures = load_pending_departures(context.keeper_state)
    if not arrivals and not departures:
        return ""
    lines: list[str] = []
    if arrivals:
        names = "、".join(name for _pid, name in arrivals)
        lines.append(
            f"- **刚到**：{names}。把他/他们**登场这件事**自然地写进这一段"
            "（推门进来、一直在外面等、跟着谁一起到的都行），别当他一直都在。"
        )
    if departures:
        names = "、".join(name for _pid, name in departures)
        lines.append(
            f"- **离场**：{names}。给一个说得通的理由让他/他们暂时退出这一幕"
            "（去外面守着、回车上拿东西、留在原地看守），**不要写死、不要写成"
            "永久离开**——他随时可能回来。"
        )
    return "桌上的人变了，而故事里还没有交代。这一段叙事里必须把下面这些圆过去：\n" + "\n".join(
        lines
    )


def render_presence(context: SituationContext) -> str:
    return format_presence(context)
