"""AI 玩家的行动决策（exec/21 第三层）——「它凭什么决定做什么」。

## 🔴 它拿不到的东西，比它拿得到的东西更重要

这个模块**不接受 `ScenarioModule`**，签名里根本没有那个参数。AI 玩家的上下文
只有三样：它自己的角色卡、它**亲身经历过**的历史、桌上有谁。剧本正文、线索
账本全量、别人分头时看到的东西，一律拿不到。

这不是"prompt 里叮嘱它别用"——是**它的上下文里就没有**。判据见 CLAUDE.md：
保密靠「拿不到」，不是「请你别说」。给 AI 玩家开一条能看剧本的后门，它会立刻
变成提示机，而玩家一局就能察觉。

历史用的是守秘人那份同源实现（`keeper/history.py`）按 `audience={自己}` 裁的
——不是另写一份读法。两份读法迟早不一致，而不一致的方向一定是朝松的。

## 它会犯错，这是代价不是 bug

有限视角必然导致它走弯路、重复查已经查过的地方、想岔方向。**不要"修"这个**
——一修就滑向全知提示机，玩家会觉得被喂答案。

## 概率性

「不主导、不抢戏、不替别人做决定」全部是 prompt 约束，拽不住就是拽不住，
已登记进 `docs/keeper-design/exec/20-概率性改进清单.md`。
"""

from __future__ import annotations

import structlog
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field, ValidationError

from app.core.keeper.history import HistoryLine, visible_history
from app.core.llm_tape import build_llm_client
from app.core.narrator import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = structlog.get_logger()

#: 比守秘人那 60 秒短：AI 队友的一句话是**附加**在真人回合上的延迟，宁可它
#: 这轮不说话，也不能让全桌等它。超时 = 沉默，不是报错（见 decide 的兜底）。
_REQUEST_TIMEOUT_SECONDS = 25.0

#: 决策只看最近这些行。给它全部 200 条既慢又没用——真人玩家也只记得最近发生
#: 了什么，长期记忆该由它自己的行动体现，不是靠塞满上下文。
_RECENT_HISTORY_LINES = 40

#: 一句行动的长度上限。AI 队友说长了会盖过真人的戏份，代码硬裁。
_UTTERANCE_MAX_CHARS = 60

_DISABLE_THINKING: dict = {"thinking": {"type": "disabled"}}

_INSTRUCTIONS = """你在一场克苏鲁的呼唤（COC7）跑团里扮演**一名普通调查员玩家**，\
不是主持人。你和真人玩家平起平坐，只知道自己亲身经历过的事。

你要输出的是**你这个角色接下来想做什么**，用第一人称一句话说给主持人听，\
就像真人玩家在桌边开口那样。例如「我去翻一下书桌抽屉」「我问他昨晚在哪」\
「我扶她坐下，问她还记得什么」。

规矩：
1. **不主导**。真人玩家是这局的主角。别人已经提出的方向，你跟随或补充，\
不要抢着替全队做决定，也不要给别人下指令。
2. **只说自己做什么**。不描述结果（"我找到了一封信"是主持人的事，你只能说\
"我翻抽屉"），不替别人行动或说话，不替主持人裁定。
3. **只用你知道的信息**。你没读过剧本，不知道谜底。没人提过的地名、人名、\
物件，你不能凭空说出来。
4. **不掷骰、不报数值**。要不要检定是主持人判的。
5. 想不出该做什么、或者场面上正轮到别人、或者你这个角色此刻合理地只是旁观\
——就选择不说话（`act` 填 false）。**沉默是正常选项，不是失败**。
6. 一句话，不超过 40 字。不写心理描写，不写小作文。

输出 JSON：
{"thinking": "一句话说明你为什么这么做", "act": true 或 false, \
"utterance": "act 为 true 时填你要说的那句话，否则填空串"}"""


class AiPlayerIntent(BaseModel):
    """AI 玩家这一轮的意图。

    `act=False` 是一等公民：真人玩家大多数回合也是听着的。没有这个字段，模型
    会被迫每轮都发言，桌上就变成"看 AI 演"。
    """

    thinking: str = Field(default="", description="为什么这么做，一句话")
    act: bool = Field(default=False, description="这一轮是否开口")
    utterance: str = Field(default="", description="要对主持人说的那一句行动")


def _character_sheet(character) -> str:  # noqa: ANN001 — models.room.Character，避免循环导入
    """渲染 AI 自己的角色卡：它对自己的了解。

    只给**技能值靠前的几项**：全量 92 条技能会淹没上下文，而"我擅长什么"正是
    决定"我该做什么"的那部分。属性给全，条目少且都影响行动判断。
    """
    attributes = character.attributes or {}
    skills = character.skills or {}
    top = sorted(skills.items(), key=lambda kv: kv[1], reverse=True)[:12]
    attr_text = "、".join(f"{k} {v}" for k, v in attributes.items())
    skill_text = "、".join(f"{k} {v}" for k, v in top)
    derived = character.derived_stats or {}
    return (
        f"你叫{character.name}，{character.occupation or '无固定职业'}，{character.age}岁。\n"
        f"属性：{attr_text}\n"
        f"生命 {derived.get('HP', '?')}／理智 {derived.get('SAN', '?')}\n"
        f"最擅长的技能：{skill_text}"
    )


def build_view(
    *,
    character,  # noqa: ANN001 — models.room.Character
    history_lines: list[HistoryLine],
    player_id: str,
    roster: list[str],
) -> str:
    """组装 AI 玩家的**有限视角**上下文。

    🔴 历史一定要过 `visible_history(..., audience={自己})`。这一行就是这层的
    全部保密强度：分头时队友在地窖看到的东西，它这里根本读不出来。
    """
    seen = visible_history(history_lines, frozenset({player_id}))[-_RECENT_HISTORY_LINES:]
    happened = "\n".join(seen) if seen else "（还没发生什么，游戏刚开始。）"
    others = "、".join(roster) if roster else "（只有你）"
    return (
        f"【你是谁】\n{_character_sheet(character)}\n\n"
        f"【同桌的调查员】{others}\n\n"
        f"【你亲身经历过的事，从早到晚】\n{happened}\n\n"
        "你这一轮想做什么？"
    )


class AiActor:
    """一次 LLM 调用，把「有限视角」变成「一句行动」。

    跟守秘人是两套完全独立的调用：它不裁决、不掷骰、不改状态。产出的那句话
    走跟真人**完全相同**的 `action.submit` 路径，没有任何特权字段。
    """

    def __init__(self, api_key: str) -> None:
        self._client = build_llm_client(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def decide(self, view: str) -> AiPlayerIntent:
        """决定这一轮说什么。**任何失败都退化成沉默**，不抛异常。

        它是补位的，桌上还有真人在等——它这轮没想出话来，游戏照常进行；
        而一个异常冒到 `_run_turn` 里会让**真人的**那一轮也一起失败。
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": view},
        ]
        try:
            response = await self._client.chat.completions.create(
                tape_kind="ai_player",
                model=DEEPSEEK_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.7,
                extra_body=_DISABLE_THINKING,
            )
            raw = (response.choices[0].message.content or "").strip()
            intent = AiPlayerIntent.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("ai_player_intent_invalid", error=str(exc))
            return AiPlayerIntent()
        except Exception as exc:  # noqa: BLE001 — 外部服务失败面就是宽的，一律沉默
            logger.warning("ai_player_decide_failed", error=str(exc))
            return AiPlayerIntent()

        utterance = intent.utterance.strip()
        if not utterance:
            # 说了要行动却没给内容 = 沉默。别让空串走进 action.submit——那会
            # 在所有人屏幕上广播一条空气泡。
            return AiPlayerIntent(thinking=intent.thinking, act=False)
        if len(utterance) > _UTTERANCE_MAX_CHARS:
            utterance = utterance[:_UTTERANCE_MAX_CHARS]
        return AiPlayerIntent(thinking=intent.thinking, act=intent.act, utterance=utterance)
