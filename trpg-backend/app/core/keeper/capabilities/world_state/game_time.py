"""游戏内时间：把「第2天 夜晚」这个自由文本变成代码能比的东西。

## 🔴 根因不是"模型偷懒"（2026-08-14 真人实测）

规则 4d 早就要求「每轮维护游戏内时间，剧情推进到新时段就更新」。实测那一局
**整整只更新过一次**（玩家说要睡觉那次），之后跑了 5 个地方、等了一小时、
开了两小时车，仍然停在「第2天 清晨」。

查下来结论很直接：**没有任何代码路径会因为时间不动而出问题**，所以它不动
也没人知道。而议程事件的触发时机正是靠它判断的。

这跟项目里那条判据是同一件事：**加了字段没有消费方 = 没加**。时间此前是一个
纯粹写给模型自己看的字符串。

## 做法：不换存储，给它一个消费方

存储仍是 `keeper_state["游戏内时间"]` 那个字符串——换形态要迁移在跑的房间，
而问题不在形态上。这里加的是**解析 + 两条代码判得了的检查**：

- **倒流**：新值比旧值早（第2天早上 → 第1天夜里）。这是纯粹的记账错误，
  代码判得了，直接拒。
- **停滞**：连续多少轮没推进。同 `无进展轮数` 的形态——把数摆到模型眼前。

解析不出来的（模组用了别的写法、模型换了措辞）一律**放行**，只是这两条检查
对它不生效——不认识的格式不该变成"你不许写"。
"""

from __future__ import annotations

import re

#: `keeper_state` 里存时间的那个键。
#:
#: 🔴 **它不是保留键**：时间仍然由模型通过 `state_updates` 维护（规则 4d），
#: 代码只在写入那一刻做检查。改成保留键就得另开一条写入路径，而"谁来推进
#: 时间"本来就是模型的判断——能确定化的是判断的**输入与校验**，不是判断本身。
GAME_TIME_KEY = "游戏内时间"

#: 一天之内的时段，**按先后排序**。判"倒流"只需要这个序，不需要真的钟点。
#:
#: 同义写法并到同一档：模型不会每次都用同一个词，而"傍晚"与"黄昏"之间分个
#: 先后没有意义——**分不出来的就别硬分**，宁可判成"没变"也不要判成"倒流"。
_PERIODS: tuple[tuple[str, ...], ...] = (
    ("凌晨",),
    ("清晨", "早晨", "一早"),
    ("上午", "早上"),
    ("中午", "正午"),
    ("下午",),
    ("傍晚", "黄昏", "日落"),
    ("晚上", "夜晚", "入夜"),
    ("深夜", "半夜", "午夜"),
)

_DAY_RE = re.compile(r"第\s*(\d+)\s*天")


def parse_game_time(text: str | None) -> tuple[int, int] | None:
    """`"第2天 夜晚"` → `(2, 6)`。认不出来返回 `None`（放行，不报错）。

    只要**天**或**时段**认出来一个就算数：模组里「第3天」「夜里」都出现过。
    认不出的那一半用 0 占位——它只参与比较，不回写。
    """
    if not text:
        return None
    day_match = _DAY_RE.search(text)
    day = int(day_match.group(1)) if day_match else 0

    period = 0
    for index, aliases in enumerate(_PERIODS, start=1):
        if any(alias in text for alias in aliases):
            period = index
            break

    if day == 0 and period == 0:
        return None
    return (day, period)


def goes_backwards(previous: str | None, incoming: str) -> bool:
    """新值是不是比旧值**早**。两边有一边认不出来就返回 False（放行）。

    平局（同一天同一时段）不算倒流——那只是这一轮没推进，很正常。
    """
    before = parse_game_time(previous)
    after = parse_game_time(incoming)
    if before is None or after is None:
        return False
    # 只认出时段没认出天时，天都是 0，比较自然退化成只比时段。
    return after < before
