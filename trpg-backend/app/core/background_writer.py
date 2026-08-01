"""给「一键生成」的角色卡写一段过去（exec/23 #55 遗留的另一半）。

## 为什么要有这个

#55 修掉的是"守秘人拿不到角色卡所以现编个人史"。修完之后空背景**不再**产生
编造——真实 API 探针里模型把空白明确留给了玩家。所以这里解决的不是 bug，是
体验：一键生成的卡规则上完全合法，却没有过去，问它「我是谁」只能得到职业。

## 🔴 只吃元数据，不吃剧本正文

喂给模型的模组信息只有 `meta.era` 和 `meta.tone`（时代与基调）。`kp_truth`、
`nodes`、`facts` 一概不碰——一来版权红线（第三方模组正文不得离开
`模组资料/`），二来**建卡阶段的玩家不该知道谜底**，让写背景的模型读了剧本，
它写出来的"重要之人"就可能正好是凶手。

## 失败就是空背景，不是报错

写背景是锦上添花，建卡才是玩家在等的事。任何失败（没配 key、超时、模型胡说、
JSON 崩了）都退化成"这张卡没有背景"——那正是本功能上线前的状态，而 #55 已经
证明那个状态是安全的。**绝不能让一次可选的润色阻塞建卡。**

## 伤疤与恐惧症留空

`background_detail` 八项里，「伤疤与旧伤」「恐惧症与狂躁症」这两项刻意不生成。
开局的调查员不该自带旧伤和精神创伤——那是跑团过程里长出来的东西，预先填上
等于替玩家把还没发生的故事写完了。剩下六项才是"他是谁"。
"""

from __future__ import annotations

import structlog
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field, ValidationError

from app.core.llm_tape import build_llm_client
from app.core.narrator import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = structlog.get_logger()

#: 比 AI 队友那 25 秒宽：建卡不在对局回合里，没有人在牌桌上等这一拍，
#: 而玩家更希望"慢一点但有内容"而不是"快但又空了"。
_REQUEST_TIMEOUT_SECONDS = 40.0

#: 单个字段的长度上限。背景是给守秘人当参考的，不是小说——`sheet_digest`
#: 那边还会再裁到 80 字，这里先拦住离谱的长篇。
_FIELD_MAX_CHARS = 60

#: 总述字段的上限，比单项宽一些（它要串起六项）。
_SUMMARY_MAX_CHARS = 120

_DISABLE_THINKING: dict = {"thinking": {"type": "disabled"}}

#: 生成哪些键 → 它们在 `background_detail` 里的键名。键名必须与前端表单一致
#: （见 `keeper/sheet_digest.py` 的 `_BACKGROUND_LABELS`），否则渲染时会退化成
#: 显示原始英文键。故意不含 injuries / phobias，理由见模块 docstring。
_DETAIL_KEYS = (
    "personalDescription",
    "ideology",
    "significantPeople",
    "meaningfulLocations",
    "treasuredPossessions",
    "traits",
)

_INSTRUCTIONS = """你在为一场克苏鲁的呼唤（COC7）跑团里的调查员写背景设定。

玩家选择了「一键生成」——他不想填表，但希望自己的角色是个**具体的人**而不是\
一串数值。你要根据他的职业、年龄、擅长的技能，写出这个人的过去。

规矩：
1. **从数值长出来**。他擅长什么，就说明他过去做过什么。别写跟技能无关的经历。
2. **普通人**。COC7 的调查员是被卷进来的普通人，不是英雄、不是超能力者、\
不是已经知道神话真相的人。绝对不要提及任何神秘学、邪教、怪物、超自然事件。
3. **留白**。写他是谁、他在乎什么，不要写他"正在调查某案"或"最近遇到怪事"\
——故事还没开始，那是主持人的事。
4. **具体但不冗长**。每项一句话，有名字有地点有细节，不要"他是个善良的人"\
这种空话。
5. 中文。人名地名符合给定的时代与地区。

输出 JSON，七个字段全部填写：
{
  "summary": "两三句话的总述，串起这个人是谁",
  "personalDescription": "形象：外貌与给人的第一印象",
  "ideology": "信念：他信什么、什么支撑着他",
  "significantPeople": "重要之人：一个具体的人，写清关系",
  "meaningfulLocations": "重要之地：一个具体地点，写清为什么",
  "treasuredPossessions": "宝贵之物：一件具体物品，写清来历",
  "traits": "特质：一个鲜明的性格或习惯"
}"""


class CharacterBackground(BaseModel):
    """生成出来的背景。字段名与 `background_detail` 的键一一对应。

    全部给默认空串：模型漏填一项时，那一项显示为「未填写」，其余五项照常
    可用——比整张卡因为一个字段缺失而退回无背景要好。
    """

    summary: str = Field(default="", description="总述，写进 character.background")
    personalDescription: str = Field(default="", description="形象")  # noqa: N815
    ideology: str = Field(default="", description="信念")
    significantPeople: str = Field(default="", description="重要之人")  # noqa: N815
    meaningfulLocations: str = Field(default="", description="重要之地")  # noqa: N815
    treasuredPossessions: str = Field(default="", description="宝贵之物")  # noqa: N815
    traits: str = Field(default="", description="特质")


def build_prompt(
    *,
    name: str,
    occupation: str,
    age: int,
    top_skills: list[tuple[str, int]],
    era: str | None,
    tone: str | None,
) -> str:
    """组装写背景用的输入。

    🔴 参数表就是这个功能的保密边界：这里**没有** `ScenarioModule`，只有时代与
    基调两个标量。想加剧本内容进来时，先回去读模块 docstring。
    """
    skill_text = "、".join(f"{n} {v}" for n, v in top_skills) or "（没有突出的技能）"
    setting_parts = [p for p in (era, tone) if p]
    setting = "；".join(setting_parts) if setting_parts else "1920 年代美国新英格兰"
    return (
        f"角色名：{name}\n"
        f"职业：{occupation}\n"
        f"年龄：{age}\n"
        f"最擅长的技能：{skill_text}\n"
        f"故事的时代与基调：{setting}\n\n"
        "请为这个调查员写背景。"
    )


def _clip(text: str, limit: int) -> str:
    return text.strip()[:limit]


class BackgroundWriter:
    """一次 LLM 调用，把一张数值卡变成一个人。

    跟守秘人、AI 玩家都是互相独立的调用：它不读对局状态、不写任何账本，
    产出只有一个 `CharacterBackground`。
    """

    def __init__(self, api_key: str) -> None:
        self._client = build_llm_client(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def write(self, prompt: str) -> CharacterBackground | None:
        """写一段背景。**任何失败都返回 None**，由调用方保持背景为空。

        返回 None 而不是空 `CharacterBackground`：调用方要能区分"模型写了但
        写得空"和"根本没写成"——前者原样落库，后者不该覆盖玩家可能已有的内容。
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ]
        try:
            response = await self._client.chat.completions.create(
                tape_kind="character_background",
                model=DEEPSEEK_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=1.0,
                extra_body=_DISABLE_THINKING,
            )
            raw = (response.choices[0].message.content or "").strip()
            background = CharacterBackground.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("character_background_invalid", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — 外部服务失败面就是宽的，一律降级成空背景
            logger.warning("character_background_failed", error=str(exc))
            return None

        return CharacterBackground(
            summary=_clip(background.summary, _SUMMARY_MAX_CHARS),
            **{k: _clip(getattr(background, k), _FIELD_MAX_CHARS) for k in _DETAIL_KEYS},
        )


def to_detail(background: CharacterBackground) -> dict[str, str]:
    """`CharacterBackground` → `character.background_detail` 的存储形状。

    只放非空项：空串写进去之后，`sheet_digest._background` 会把它跳过，但
    数据库里留一堆空键既没意义又让人以为"填过了"。
    """
    return {key: value for key in _DETAIL_KEYS if (value := getattr(background, key).strip())}
