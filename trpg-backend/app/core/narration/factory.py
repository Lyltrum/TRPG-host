"""按配置组装出一个 `Narrator`（组装层，天然在依赖图顶端）。

🔴 工厂**必须**依赖全部实现——这正是它存在的理由：把"选哪个实现"这件事
从抽象层里赶出来。加一个新实现时改这里，不改 `contract.py`。
"""

from pathlib import Path

from app.core.config import Settings
from app.core.narration.contract import Narrator
from app.core.narration.deepseek import DeepSeekNarrator
from app.core.narration.delayed import DelayedNarrator
from app.core.narration.fallback import FallbackNarrator
from app.core.narration.room_aware import RoomAwareKeeperNarrator


def build_narrator(settings: Settings) -> Narrator:
    """按配置选择实现（优先级从高到低）：

    1. `deepseek_api_key` 且（catalog 目录有 structured 或配置了
       `keeper_module_path`）→ RoomAwareKeeperNarrator（按房间选剧本）；
    2. 只配 `deepseek_api_key` → DeepSeekNarrator（单轮叙事）；
    3. 都没配 → FallbackNarrator（占位文案，CI/e2e 零外部依赖）。

    `narrator_delay_seconds > 0` 时再包一层 DelayedNarrator（测试钩子）。
    """
    from app.core.keeper.contract.catalog import default_modules_dir

    narrator: Narrator
    modules_dir = (
        Path(settings.keeper_modules_dir).expanduser().resolve()
        if settings.keeper_modules_dir
        else default_modules_dir()
    )
    fallback = (
        Path(settings.keeper_module_path).expanduser().resolve()
        if settings.keeper_module_path
        else None
    )
    # 🔴 显式配了 keeper_module_path 却指不到文件 = 配置错误，**启动期就炸**。
    # 不能只在 RoomAwareKeeperNarrator._resolve 里推迟到玩家第一次发言才报——那会把"服务起不来"
    # 这种一眼可见的故障，变成"对局跑到一半 AI 突然失灵"。
    if fallback is not None and not fallback.is_file():
        raise FileNotFoundError(f"KEEPER_MODULE_PATH 指向的剧本不存在：{fallback}")

    # 启用 keeper：有 key，且至少有一个可加载的 structured（catalog 映射或兜底）
    from app.core.keeper.contract.catalog import KEEPER_MODULE_SPECS, resolve_structured_path

    any_structured = any(
        resolve_structured_path(modules_dir, s.scenario_id) is not None for s in KEEPER_MODULE_SPECS
    )
    keeper_ready = bool(settings.deepseek_api_key) and (any_structured or fallback is not None)

    if keeper_ready:
        from app.core.db import async_session_factory

        assert settings.deepseek_api_key is not None
        narrator = RoomAwareKeeperNarrator(
            settings.deepseek_api_key,
            modules_dir=modules_dir,
            fallback_path=fallback,
            session_factory=async_session_factory,
        )
    elif settings.deepseek_api_key:
        narrator = DeepSeekNarrator(settings.deepseek_api_key)
    else:
        narrator = FallbackNarrator()
    if settings.narrator_delay_seconds > 0:
        narrator = DelayedNarrator(narrator, settings.narrator_delay_seconds)
    return narrator
