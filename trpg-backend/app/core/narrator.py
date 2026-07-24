"""AI 主持人叙事生成（issue #107 地基）。

⚠️ 定位：这里只做"单轮叙事生成"——给定玩家这一句话 + 一点上下文，生成一段
KP（守秘人）风格的场景描写。**不做 AI 编排**：意图解析（这句话是行动还是
提问？）、规则裁决（要不要掷骰、成功与否）、世界状态管理（场景/线索/进度）
都不是本模块的事，那是 #48/#68 编排层的职责范围。

之所以先落这一层薄的抽象，是为了让 `action.submit`（#107 定稿后玩家跟 AI
说话的唯一事件——"是行动还是提问"由 AI 判断，协议层不预分类，所以没有
单独的 dm.ask；后续批次接线）能从"占位文案"平滑切到"真实大模型叙事"，
而不必等编排层整体就位——
本模块实现的 `Narrator.narrate()` 接口，就是未来编排层要接管的位置：编排层
上线后，`build_narrator` 会被替换成"先跑意图解析/规则裁决，再调用编排后的
叙事生成"，调用方（WS 层）不需要感知这层切换。

`FallbackNarrator` 是没有配置 API Key 时的确定性占位实现，保证 CI/e2e 不
依赖外部服务；`DeepSeekNarrator` 是接了真实大模型的实现。两者行为完全由
`build_narrator` 按配置二选一，调用方只认 `Narrator` 这一个接口。
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import Settings

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
_REQUEST_TIMEOUT_SECONDS = 30.0

_SYSTEM_PROMPT = (
    "你是一名跑《克苏鲁的呼唤》（COC7）跑团的守秘人（KP）。"
    "根据玩家刚才的行动，生成一段简短（150 字以内）的叙事回应，"
    "延续克苏鲁风格的诡异、压抑、未知的恐怖氛围。"
    "你只做场景描写和气氛推进：不要替玩家做决定，不要掷骰，"
    "不要裁定行动是否成功——那些是规则引擎的职责，不属于你。"
)


@dataclass(frozen=True, slots=True)
class NarrationContext:
    """生成一段叙事所需的全部上下文。调用方（WS 层）负责准备好这些字段——
    本模块不查库，也不知道房间/玩家在数据库里长什么样。"""

    utterance: str
    player_nickname: str
    module_title: str | None = None
    # 每条已格式化成"昵称: 内容"形状，由调用方从 `events` 表整理出来。
    recent_actions: list[str] = field(default_factory=list)
    # keeper agent（feat/keeper-agent 实验）需要的定位信息：它的工具要知道
    # 在哪个房间、为谁掷骰/改状态。单轮叙事实现（DeepSeek/Fallback）不读。
    room_id: str | None = None
    player_id: str | None = None
    # 世界心跳主动轮（路线 6）：裁决/叙事走克制模式，不发起检定。
    is_heartbeat: bool = False


@dataclass(frozen=True, slots=True)
class CheckRequestNotice:
    """一次"待掷检定"的通知（两段式玩家掷骰）——守秘人裁决需要检定后不立即
    掷骰，而是把这条通知随叙事一起广播，玩家在前端点击确认后才真正掷骰。"""

    check_request_id: str
    kind: str  # "skill" | "san"
    player_id: str
    player_nickname: str
    skill: str | None  # kind="san" 时为 None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CheckResultNotice:
    """一次检定的服务端权威结果（玩家点击掷骰确认后产生）。"""

    check_request_id: str
    kind: str  # "skill" | "san"
    player_id: str
    skill: str | None  # kind="san" 时为 None
    rolled: int
    target: int
    level: str  # skill：成功等级文本；san："成功"/"失败"
    san_loss: int | None = None
    san_remaining: int | None = None


@dataclass
class NarrationOutcome:
    """`Narrator.narrate()`/`resolve_check()` 的统一返回形状。

    `text` 是要广播的叙事（两段式掷骰的"重发请求"分支可能为空串——彼时不该
    广播一条空 narration.push）；`check_requests`/`check_results` 是本轮新
    发起/新结算的检定通知，调用方（WS 层）负责把它们各自广播成
    `check.request`/`check.result` 事件。非 keeper 的单轮叙事实现两个列表
    恒为空。"""

    text: str
    check_requests: list[CheckRequestNotice] = field(default_factory=list)
    check_results: list[CheckResultNotice] = field(default_factory=list)


class Narrator(ABC):
    """叙事生成器接口。"""

    @abstractmethod
    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        """根据上下文生成一段叙事文本（及本轮的检定请求/结果通知）。"""

    async def resolve_check(
        self, room_id: str, player_id: str, check_request_id: str
    ) -> NarrationOutcome:
        """结算一次玩家确认的掷骰（两段式玩家掷骰）。

        默认不支持：单轮叙事实现（Fallback/DeepSeek）没有"待掷检定"的概念，
        WS 层收到 check.roll/san.check.roll 时应把 NotImplementedError 转成
        NOT_IMPLEMENTED 错误事件，而不是让它把整条连接炸掉。
        """
        raise NotImplementedError


class FallbackNarrator(Narrator):
    """无 API Key 时的确定性占位实现，等价于此前 ws.py 里硬编码的占位文案。"""

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        return NarrationOutcome(text=f"守秘人记下了你的行动：「{context.utterance}」……")


def _build_messages(context: NarrationContext) -> list[ChatCompletionMessageParam]:
    """把 `NarrationContext` 拼成 chat completion 的 messages。

    抽成独立的纯函数，是为了不发真实网络请求也能单测 prompt 组装是否正确
    （`tests/test_narrator.py`）。
    """
    lines = []
    if context.module_title:
        lines.append(f"当前模组：{context.module_title}")
    if context.recent_actions:
        lines.append("最近的行动：")
        lines.extend(context.recent_actions)
    lines.append(f"{context.player_nickname}：{context.utterance}")
    user_content = "\n".join(lines)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


class DeepSeekNarrator(Narrator):
    """接真实 DeepSeek 大模型的叙事生成实现。

    超时/请求失败一律让异常抛出去，本模块不吞——由调用方（WS 层，#107 后续
    批次接线）决定失败时怎么回应玩家（比如退回占位文案，或者回一条 error
    事件），职责不在这里。
    """

    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        response = await self._client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=_build_messages(context),
        )
        return NarrationOutcome(text=response.choices[0].message.content or "")


class DelayedNarrator(Narrator):
    """测试专用包装：narrate 前人为等待一段时间（issue #107 测试钩子）。

    为什么需要它：FallbackNarrator 同步秒回，`action.submit` 的房间锁窗口只有
    微秒级——e2e 里两个客户端"同时提交"永远压不中 `ACTION_IN_PROGRESS`，
    锁相关的验收全成了测不到的死代码。配置 `narrator_delay_seconds`（生产
    永远是 0）后锁窗口被人为拉长，并发拒绝路径才能被稳定命中。
    """

    def __init__(self, inner: Narrator, delay_seconds: float) -> None:
        self._inner = inner
        self._delay_seconds = delay_seconds

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        await asyncio.sleep(self._delay_seconds)
        return await self._inner.narrate(context)

    async def resolve_check(
        self, room_id: str, player_id: str, check_request_id: str
    ) -> NarrationOutcome:
        await asyncio.sleep(self._delay_seconds)
        return await self._inner.resolve_check(room_id, player_id, check_request_id)


def build_narrator(settings: Settings) -> Narrator:
    """按配置选择实现（优先级从高到低）：

    1. `keeper_module_path` + `deepseek_api_key` 都配了 → KeeperAgent（真正的
       守秘人 agent，feat/keeper-agent 实验）；剧本加载失败时**故意让异常抛出、
       应用启动失败**——配了 keeper 就是要玩 keeper，静默回退到单轮叙事只会
       让人误以为在测 agent；
    2. 只配 `deepseek_api_key` → DeepSeekNarrator（单轮叙事）；
    3. 都没配 → FallbackNarrator（占位文案，CI/e2e 零外部依赖）。

    `narrator_delay_seconds > 0` 时再包一层 DelayedNarrator（测试钩子，见其
    docstring），生产配置保持 0、不受影响。
    """
    narrator: Narrator
    if settings.keeper_module_path and settings.deepseek_api_key:
        # 局部 import：keeper 包依赖 agents SDK 与 DB 层，只有真配了 keeper
        # 模式才值得付出这次导入；也避免 narrator（core 底层）无条件反向
        # 依赖更上层的模块。
        from app.core.coc7_content import build_coc7_ruleset
        from app.core.db import async_session_factory
        from app.core.keeper.agent import KeeperAgent
        from app.core.keeper.module_loader import load_module

        narrator = KeeperAgent(
            api_key=settings.deepseek_api_key,
            module=load_module(settings.keeper_module_path),
            ruleset=build_coc7_ruleset(),
            session_factory=async_session_factory,
        )
    elif settings.deepseek_api_key:
        narrator = DeepSeekNarrator(settings.deepseek_api_key)
    else:
        narrator = FallbackNarrator()
    if settings.narrator_delay_seconds > 0:
        narrator = DelayedNarrator(narrator, settings.narrator_delay_seconds)
    return narrator
