"""L3 召回：玩家问一件过去的事时，把原文从 `events` 里查回来（`exec/47` P2）。

## 为什么会有这一层

L3 是**滑动窗口**：超过 `HISTORY_LIMIT` 的那些不再注入。但**原文一个字都没丢**
——滚出去的是"注入"，不是"存储"。

🔴 实锤（2026-08-24，房间 `0BQ1Q2`）：玩家问「借书卡夹在第几页」，模型确信地
答「第 87 页」并补了一张不存在的插页；而库里那一段原文写着 88，`events` 表里
出现了 **6 次**。**系统不是不知道，是这一拍没把它拿出来。**

08-23 曾以「存都没存下来，召回再准也捞不回来」把召回整个降级——那句话**只对
L2 成立**（散文压缩丢了就是丢了），推广到 L3 是拿错了度量对象。

## 判据

- **只在这一拍召回**：触发是裁决写下的 `recall_query`（玩家在打听过去的事），
  常规拍一个字都不多注入 ⇒ 不动 system prompt、前缀缓存不受影响。
- **不另写一份历史读法**：语料走 `history_lines_from_events` + `visible_history`,
  跟 L3 与 AI 玩家**同一份**。另写一份的话两边迟早不一致，而不一致的方向
  一定是"看到了不该看的"（同 `history.py` 开头那段）。
- **受众照裁**：召回按这一段的受众取交集。分头时门厅那段召不回地下室的原文
  ——保密靠"拿不到"，不是"请你别说"。
- **不用向量库**：本房间语料只有一两千行、带 `room_id` 边界、`audience` 字段
  现成，BM25 够用**而且可解释**（命中哪几行看得见）。见 `exec/48 §5`。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.memory.history import (
    HISTORY_EVENT_TYPES,
    HistoryLine,
    history_lines_from_events,
    visible_history,
)
from app.models.event import Event
from app.models.room import Player

logger = structlog.get_logger()

#: 召回几行。3 行的实测成本是 68–323 字符（中位 224），对照分层注入之后的长模组
#: system prompt 是 12,430 —— 这一段进的是局面块，量级上可以忽略。
RECALL_TOP_K = 3

#: 「这一问值不值得给召回段」的门槛，量的是**最高分那一行覆盖了几成查询词**。
#:
#: 🔴 **不能用 BM25 的绝对分数当门**：那个数随语料规模变——同一句真命中，
#: 560 行的语料里是 66.8 分，而只有 1 行时 IDF 把它压到 0.29。拿绝对分数设门，
#: 换个房间就换一套行为（同判据「隔着模型的量，别调阈值」）。覆盖率与规模无关。
#:
#: **标定数据**（2026-08-24，房间 `0BQ1Q2` 那 560 行真语料，8 正 3 负）：
#: 正样本 top1 覆盖率 **0.50–0.75**，负样本（问本局没发生过的事）**0.00–0.29**。
#: 取 0.4 ⇒ 两侧各留约 1.3–1.7 倍余量。
MIN_QUERY_COVERAGE = 0.4

#: 中文分词在这里是**字符 bigram**，不引第三方分词器：语料是一两千行的小集合，
#: bigram 的召回率足够（离线 top5 10/10），而且没有词典就没有"词典没收录"的坑。
_PUNCT = re.compile(r"[\s，。、；：？！“”‘’（）《》【】…—\-·,.;:?!\"'()\[\]]")

#: 疑问句里的口水词。留着的话每条查询都会去匹配"我""的""来着"，噪声压过信号。
_STOP = set(
    "我你他她它的了是在有没不个这那么什来着吧呢啊呀嘛和跟与就都还也很多少几"
    "怎样如何哪里什么当时之前后来一下上去过把被让给对从到位于时候记得知道"
)


def tokenize(text: str) -> list[str]:
    """字符 bigram + 独立的数字/西文片段。

    🔴 数字与西文**必须单独成词**：车牌 `KX-4471`、页码 `88` 这类正是玩家最会
    回来问的东西，而 bigram 会把它们切碎。
    """
    cleaned = _PUNCT.sub("", text)
    chars = [c for c in cleaned if c not in _STOP]
    joined = "".join(chars)
    tokens = [joined[i : i + 2] for i in range(len(joined) - 1)]
    tokens += [c for c in chars if c.isascii() and c.isalnum()]
    return tokens


class _BM25:
    """标准 BM25，语料小到可以每次现建（一两千行，毫秒级）。"""

    def __init__(self, docs: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self._toks = [tokenize(d) for d in docs]
        self._tf = [Counter(t) for t in self._toks]
        self._lens = [len(t) for t in self._toks]
        self._avg = sum(self._lens) / max(1, len(self._lens))
        self._df: Counter[str] = Counter()
        for toks in self._toks:
            for term in set(toks):
                self._df[term] += 1
        self._n = len(docs)
        self._k1, self._b = k1, b

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for i in range(self._n):
            s = 0.0
            for term in q:
                f = self._tf[i].get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (self._n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                denom = f + self._k1 * (1 - self._b + self._b * self._lens[i] / self._avg)
                s += idf * f * (self._k1 + 1) / denom
            out.append(s)
        return out


def _coverage(query: str, text: str) -> float:
    """这一行覆盖了几成查询词元。与语料规模无关，所以拿它当门。"""
    q = set(tokenize(query))
    if not q:
        return 0.0
    return len(q & set(tokenize(text))) / len(q)


def rank(lines: list[str], query: str, *, top: int = RECALL_TOP_K) -> list[str]:
    """从已经裁好受众的历史行里挑最相关的几行。纯函数，不碰 IO。

    🔴 **门只看最高分那一行**，其余几行跟着走。逐行设门是不行的——标定数据里
    「纸条上写着什么」那一问，真正含答案的行覆盖率只有 0.25，比负样本的最高值
    （0.29）还低；而它的 top1 有 0.50。**整段给不给**是一个判断，**给哪几行**
    是另一个，混成一个阈值就会把真答案删掉。

    保持**原文顺序**返回：召回段是给模型读的历史片段，按时间读比按分数读自然。
    """
    if not lines or not query.strip():
        return []
    scored = _BM25(lines).scores(query)
    order = sorted(range(len(lines)), key=lambda i: scored[i], reverse=True)
    best = order[0]
    if scored[best] <= 0 or _coverage(query, lines[best]) < MIN_QUERY_COVERAGE:
        # 问的是本局没发生过的事 ⇒ **什么都不给**。硬塞几行不相干的历史正是
        # 编造的原料，比不给更糟。
        return []
    picked = [i for i in order[:top] if scored[i] > 0]
    return [lines[i] for i in sorted(picked)]


async def _load_all_history(db: AsyncSession, room_id: str) -> list[HistoryLine]:
    """整局的历史行——**不设 limit**，这正是召回存在的理由。

    L3 那条查询带 `limit(HISTORY_LIMIT)`，而这里要的恰恰是**滚出窗口的那些**。
    """
    nick_rows = await db.execute(
        select(Player.id, Player.nickname).where(Player.room_id == room_id)
    )
    nicknames = {str(pid): str(nick) for pid, nick in nick_rows.all()}
    rows = await db.execute(
        select(Event)
        .where(Event.room_id == room_id, Event.event_type.in_(HISTORY_EVENT_TYPES))
        .order_by(Event.created_at, Event.id)
    )
    return history_lines_from_events(list(rows.scalars()), nicknames)


async def recall_history(
    db: AsyncSession,
    *,
    room_id: str,
    query: str,
    audience: frozenset[str] | None,
    top: int = RECALL_TOP_K,
) -> list[str]:
    """这一段的受众能看见的历史里，跟 `query` 最相关的几行原文。"""
    if not query.strip():
        return []
    lines = await _load_all_history(db, room_id)
    visible = visible_history(lines, audience)
    hits = rank(visible, query, top=top)
    logger.info(
        "keeper_memory_recall",
        room_id=room_id,
        # 🔴 只记数字与查询词，**不记召回的正文**——那里面有剧本内容与私密原话，
        #    同 `context_budget` 那条"只记数字不记内容"。
        query=query[:60],
        corpus=len(visible),
        hits=len(hits),
        chars=sum(len(h) for h in hits),
    )
    return hits


def format_recall(hits: list[str]) -> str:
    """召回段。空的时候返回空串 —— 调用方据此整段跳过。

    🔴 **明写"没查到就说记不清"**：这一段的全部意义是把编造换成真原文，而
    "召回不到时该怎么办"如果不写，模型仍然会拿一个像样的值填空（一条规则写完
    先问它有没有反方向）。用户 2026-08-24 的判据是**不许拿"记不清"当主修法**
    ——所以它只出现在这里，作为召回落空之后的兜底。
    """
    if not hits:
        return ""
    body = "\n".join(f"- {h}" for h in hits)
    return (
        "## 他问的那件事，历史原文在这里（已滚出窗口，按这一问查回来的）\n"
        f"{body}\n"
        "🔴 回答里的具体值（数字、名字、外号、位置）**只能照这几行原文写**，"
        "一个字都不要改。这几行里没有的，就说记不清、让他自己定，**不要编一个像样的**。\n"
    )
