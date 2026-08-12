"""局域网开局：私有网段的 Origin 放行，公网的不放行。

起因：邀请链接/二维码（`exec/35 §4`）做完之后仍然发给朋友打不开——链接对了，
但朋友的浏览器发出的 Origin 是 `http://192.168.x.x:9877`，固定清单里不可能有它。

🔴 这条测试是**两头都验**的：只断言"私有网段放行"的话，把正则写成 `.*` 也会
绿。放行范围必须**同时**被证明是收着的。
"""

from __future__ import annotations

import re

import pytest

from app.core.config import PRIVATE_NETWORK_ORIGIN_REGEX

_PATTERN = re.compile(PRIVATE_NETWORK_ORIGIN_REGEX)


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.1.5:9877",  # 家里路由器最常见的网段
        "http://10.0.0.7:9877",
        "http://172.16.3.9:9877",
        "http://172.31.255.254:9877",
        "http://localhost:9877",
        "http://127.0.0.1:9877",
        "http://192.168.1.5",  # 不带端口（80）
    ],
)
def test_devices_in_the_same_room_are_allowed(origin: str) -> None:
    assert _PATTERN.match(origin), origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "https://evil.com",
        # 🔴 前缀/后缀骗过正则的经典两招：正则没锚定就会放行它们
        "http://192.168.1.5.evil.com",
        "http://evil.com/192.168.1.5",
        "http://notlocalhost:9877",
        # 172.15 / 172.32 在私有网段之外（私有段只有 172.16–172.31）
        "http://172.15.0.1:9877",
        "http://172.32.0.1:9877",
        # 局域网里没有证书，https 的私有地址不是这条规则要服务的场景
        "https://192.168.1.5:9877",
    ],
)
def test_everything_else_is_not(origin: str) -> None:
    assert not _PATTERN.match(origin), origin


async def test_the_middleware_actually_answers_a_lan_preflight(client) -> None:
    """🔴 「加了字段没有消费方 = 没加」：上面全是对正则本身的断言，正则写对了
    但没接进 `CORSMiddleware` 的话一条都不会红。这条走真实的 app。"""
    response = await client.options(
        "/api/v1/games",
        headers={
            "Origin": "http://192.168.1.5:9877",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://192.168.1.5:9877"


async def test_a_public_origin_gets_no_allow_header(client) -> None:
    response = await client.options(
        "/api/v1/games",
        headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_the_switch_can_turn_it_off() -> None:
    """默认开着（定位就是"自己和朋友在一间屋子里玩"），但要留得下关掉的路。"""
    from app.core.config import Settings

    assert Settings().cors_allow_private_network is True
    assert Settings(cors_allow_private_network=False).cors_allow_private_network is False
