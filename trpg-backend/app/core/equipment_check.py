"""建卡时的装备合理性校验：这几件东西，这个人在这个时代这个地方拿得到吗？

## 为什么必须是语义判断

装备是**自由文本**——玩家想写什么写什么。而"合不合理"取决于三样互相独立的
东西，任何一张词表都同时管不了：

- **年代**：1925 年没有手机；而六份模组里有两份写着「现代」，那里手机完全合法。
  `meta.era` 本身也是自由文本（实测六份的形态是「现代」「1924年7月」
  「1920 年代禁酒令时期,美国密歇根州阿诺兹堡市」），代码没法从中解析出年份。
- **地点**：同一把手枪，在美国是杂货店买得到的东西，在同年代的英国要许可证。
  角色卡上的 `residence` / `birthplace` 和模组的 `era` 里都可能带着地名。
- **身份**：警察配枪合理，图书管理员配警用装备不合理；医生带吗啡是本职，
  会计带吗啡不是。信用评级还决定了他买不买得起。

关键词黑名单在第一条上就输了：拦掉「手机」，玩家写「行动电话」就绕过去；
而且它对后两条**完全无能为力**。这正是判据全集里那条「不要用自由文本当
标识符：要么白名单 id，要么退化成同义词打地鼠」的又一个实例——只不过这次
连白名单都不存在，因为可能的物品是开放集合。

## 🔴 只吃元数据，不吃剧本正文

喂给模型的模组信息只有 `meta.era`（`tone` 2026-08-18 撤掉，理由见
`service/character_background.module_era`），理由与 `background_writer`
完全相同：版权红线（第三方模组正文不得离开 `模组资料/`），以及**建卡阶段的
玩家不该知道谜底**。参数表就是这个功能的保密边界。

## 拦截是硬的（用户 2026-08-16 拍板）

判为「拿不到」的装备**不放行**，玩家改到过为止，没有"我就要这个"的口子。
代价是模型误判时玩家只能改写措辞——这一点在拍板前明确提示过。

🔴 **但"模型说不行"和"模型没说话"是两回事**：没配 api key、超时、JSON 崩了
一律**放行**并记日志。硬拦的对象是"判断结果为不合理"，不是"判断没跑成"——
让一次外部服务抖动锁死所有人的建卡，那不是严格，是把可用性押给了第三方。
CI / e2e 环境本来就不配 key，走的正是这条放行路径。
"""

from __future__ import annotations

import structlog
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field, ValidationError

from app.core.llm_tape import build_llm_client
from app.core.narration.deepseek import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = structlog.get_logger()

#: 与写背景同一档：建卡不在对局回合里，没有人在牌桌上等这一拍。
_REQUEST_TIMEOUT_SECONDS = 40.0

_DISABLE_THINKING: dict = {"thinking": {"type": "disabled"}}

#: 一次最多判多少件。**这是兜底不是常规路径**：正常玩家写三五件，写到几十件
#: 的要么是在灌文本，要么是把背景故事整段贴进了装备栏。超出的部分不判也不拦。
_MAX_ITEMS = 30

_INSTRUCTIONS = """你在审核一张克苏鲁的呼唤（COC7）调查员卡上的随身装备清单。

你要回答的只有一个问题：**以这个人的身份，在这个时代、这个地方，他能不能\
合理地拥有这件东西？**

判「拿不到」的三类理由，只有这三类：
1. **时代对不上**：这个年代还不存在的东西（1925 年的手机、青霉素、塑料袋）。
2. **地点对不上**：那个国家/地区的法律或现实条件下普通人搞不到的东西\
（战后英国的私人手枪、非产地的管制药品）。
3. **身份对不上**：这个职业、这个信用评级的人不可能持有的东西\
（图书管理员的警用配枪、码头工人买不起的进口跑车、平民手里的军用炸药）。

判断的尺度：
- **可疑不算拿不到**。少见但说得通的一律放行——祖传的怪异护身符、\
从战场带回来的旧军刀、朋友借的相机、职业顺手能接触到的东西，都合理。
- **拿不准就放行**。你的判断会**直接挡住玩家开局**，所以只在你确信\
"这个人不可能有这个"时才判不行。
- 数量与品质不用管，只看**这件东西本身**。
- 一件东西只要有一个说得通的来路，就算合理。

对每件判为「拿不到」的东西，给出 1-2 个**同一时代同一地点的替代品**，\
要具体到能直接写进装备栏（不要写"换个别的"这种废话），并用一句话说清\
为什么原来那件不行。

输出 JSON：
{
  "rejected": [
    {
      "item": "原样抄写清单里那件东西的名字",
      "reason": "一句话，为什么这个人在这里拿不到它",
      "alternatives": ["替代品1", "替代品2"]
    }
  ]
}

全部合理时返回 {"rejected": []}。"""


class RejectedItem(BaseModel):
    """一件判为「拿不到」的装备。"""

    item: str
    reason: str = ""
    alternatives: list[str] = Field(default_factory=list)


class EquipmentVerdict(BaseModel):
    rejected: list[RejectedItem] = Field(default_factory=list)


def build_prompt(
    *,
    equipment: list[str],
    occupation: str | None,
    age: int | None,
    residence: str | None,
    birthplace: str | None,
    credit_rating: int | None,
    era: str | None,
) -> str:
    """组装审核输入。

    🔴 参数表就是这个功能的保密边界：这里**没有** `ScenarioModule`，只有时代与
    基调两个标量。想加剧本内容进来时，先回去读模块 docstring。
    """
    lines = [
        f"职业：{occupation or '未填写'}",
        f"年龄：{age if age is not None else '未填写'}",
        f"居住地：{residence or '未填写'}",
        f"出生地：{birthplace or '未填写'}",
        # 信用评级同时代表"买得起什么"和"什么阶层"，是身份那一类判断的主要依据。
        f"信用评级：{credit_rating if credit_rating is not None else '未填写'}",
        f"故事的时代：{era or '未说明'}",
        "",
        "随身装备清单：",
    ]
    lines.extend(f"- {name}" for name in equipment)
    lines.append("")
    lines.append("请审核这份清单。")
    return "\n".join(lines)


class EquipmentChecker:
    """一次 LLM 调用，判一份装备清单里有没有这个人拿不到的东西。

    跟守秘人、AI 玩家、写背景都是互相独立的调用：不读对局状态、不写任何账本。
    """

    def __init__(self, api_key: str) -> None:
        self._client = build_llm_client(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def check(self, prompt: str) -> EquipmentVerdict | None:
        """审一份清单。

        🔴 **返回 None = 没判成，不是「全都合理」**。调用方必须能区分这两者：
        前者放行（外部服务的问题不该变成玩家的问题），后者也放行但含义完全不同。
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ]
        try:
            response = await self._client.chat.completions.create(
                tape_kind="equipment_check",
                model=DEEPSEEK_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                # 🔴 低温：这是裁决不是创作。同「两阶段回合制」里裁决那一段。
                temperature=0.2,
                extra_body=_DISABLE_THINKING,
            )
            raw = (response.choices[0].message.content or "").strip()
            return EquipmentVerdict.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("equipment_check_invalid", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — 外部服务失败面就是宽的，一律放行
            logger.warning("equipment_check_failed", error=str(exc))
            return None


def clamp_items(equipment: list[str]) -> list[str]:
    """要送去审的那些。空白项剔掉，超量的截断（理由见 `_MAX_ITEMS`）。"""
    return [name for raw in equipment if (name := raw.strip())][:_MAX_ITEMS]


def rejection_message(rejected: RejectedItem) -> str:
    """一条能直接显示给玩家的说明：为什么不行 + 改成什么。

    替代品拼进同一句话，不新增 DTO 字段——`ValidationIssue` 的 message 本来就是
    "给人看的说明"，而多开一个 `alternatives` 字段要一路改到前端四层。
    """
    parts = [f"「{rejected.item}」{rejected.reason.strip() or '在这个设定下拿不到'}"]
    if rejected.alternatives:
        picks = "、".join(a.strip() for a in rejected.alternatives if a.strip())
        parts.append(f"可以改成：{picks}")
    return "；".join(parts)
