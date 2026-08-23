"""模型 / base_url / 六处超时：**环境变量必须真的说了算**。

## 🔴 为什么单独有这个文件

在这之前，模型与 base_url 是 `narration/deepseek.py` 里的模块常量，而 `.env`
里同时摆着一行 `DEEPSEEK_MODEL=deepseek-chat` —— **那一行从来没人读**。
改它不产生任何效果。

那比"没有这个配置"更糟：它看着像一个开关，实际是装饰。用户 2026-08-23 直接
点出来："这个也不应该是死配置，应该都在环境变量里来控制。"

超时那六处是同一个毛病的第二段：我当时给"不接超时"编的理由是「`.env` 里只有
一个 `DEEPSEEK_TIMEOUT_SECONDS`，对不上任何一处」——**那不是理由，多设几个键
就行了**（用户当场指出）。拿"现状不方便"当"设计如此"。

## 守什么

**不是"默认值是多少"**（那种断言会在调默认值时白白变红），而是
**"改了环境变量，真正发出去的请求会不会跟着变"**。做成模块常量就会静默失效
——常量在 import 那一刻定死，`.env` 和 `monkeypatch` 都改不动它。
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.narration.deepseek import deepseek_base_url, deepseek_model


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_model_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 这条就是那个死配置的守门人。变异：把 `deepseek_model()` 改回常量，当场红。"""
    monkeypatch.setenv("DEEPSEEK_MODEL", "some-other-model")
    assert deepseek_model() == "some-other-model"


def test_the_base_url_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid")
    assert deepseek_base_url() == "https://example.invalid"


#: 六个调用点各自的超时读取函数 → 对应的环境变量名。
#:
#: 🔴 **它们本来就该是六个不同的数**：一次裁决要跑完整条两阶段链，而 AI 玩家
#: 只是判一句"要不要接话"。拿同一个数卡两者，短的那头白等、长的那头误杀。
_TIMEOUTS = [
    ("app.core.narration.deepseek", "_request_timeout_seconds", "DEEPSEEK_TIMEOUT_SECONDS"),
    ("app.core.keeper.runtime.llm_calls", "request_timeout_seconds", "KEEPER_TIMEOUT_SECONDS"),
    ("app.core.ai_actor", "_request_timeout_seconds", "AI_ACTOR_TIMEOUT_SECONDS"),
    ("app.core.equipment_check", "_request_timeout_seconds", "EQUIPMENT_CHECK_TIMEOUT_SECONDS"),
    ("app.core.background_writer", "_request_timeout_seconds", "CHAPTER_TIMEOUT_SECONDS"),
    ("app.service.recap", "_timeout_seconds", "RECAP_TIMEOUT_SECONDS"),
]


@pytest.mark.parametrize(("module_name", "func_name", "env_var"), _TIMEOUTS)
def test_each_timeout_comes_from_its_own_environment_variable(
    module_name: str, func_name: str, env_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    module = importlib.import_module(module_name)
    read = getattr(module, func_name)
    monkeypatch.setenv(env_var, "7.5")
    assert read() == 7.5, f"{module_name}.{func_name}() 没跟着 {env_var} 走"


def test_the_six_timeouts_are_not_secretly_one() -> None:
    """🔴 **对侧**：别把六个键接成同一个 setting。

    只验"能被环境变量改"的话，六个函数全 `return get_settings().deepseek_timeout_seconds`
    这个退化实现照样全绿——那就把六处有意的差异抹平了，而抹平之后不会有任何
    东西变红（表现只是"某一处开始莫名超时"）。
    """
    import importlib

    values = set()
    for module_name, func_name, _ in _TIMEOUTS:
        module = importlib.import_module(module_name)
        values.add(getattr(module, func_name)())
    assert len(values) >= 3, f"六处超时只剩 {len(values)} 个不同的值 —— 大概率被接成同一个 setting"
