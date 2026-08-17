"""额度用完时，玩家侧看到的是什么。

## 🔴 为什么这条比闸门本身更容易坏

闸门拦住了花钱那一步，但玩家收到的如果是「守秘人暂时无法回应，请稍后重试」，
他会**照着提示一直重试**——而每次重试都再走一次记账，越试数字越大。
门是拦住了，可玩家没有任何办法知道发生了什么，也没有一条走得通的出路。

「加一道门，必须同时给它配一条走得通的修法」。这个文件守的就是那条修法。
"""

from __future__ import annotations

import ast
import pathlib

_WS = pathlib.Path(__file__).resolve().parents[1] / "app" / "controller" / "ws.py"


def _broad_turn_handlers() -> list[ast.ExceptHandler]:
    """找出那几处「一轮跑砸了」的宽捕获。

    判据是**它捕获 Exception 且体内发的是回合失败**，不是按行号写死——
    按行号钉住的守护测试在下一次插入代码时就会指错地方。
    """
    tree = ast.parse(_WS.read_text(encoding="utf-8"))
    found: list[ast.ExceptHandler] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
            continue
        called = {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if "_send_turn_failure" in called or _sends_internal_error(node):
            found.append(node)
    return found


def _sends_internal_error(node: ast.AST) -> bool:
    for call in ast.walk(node):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            continue
        if call.func.id != "_send_error":
            continue
        for arg in call.args:
            if isinstance(arg, ast.Constant) and arg.value == "INTERNAL_ERROR":
                return True
    return False


def test_every_turn_failure_path_distinguishes_quota_from_a_real_error() -> None:
    """🔴 没有任何一处回合失败还在直接发 INTERNAL_ERROR。

    这条会在**加第四处宽捕获时**变红——那正是它存在的理由：新加的那处如果
    照抄旧写法，额度用完又会被报成"服务器出错"，而这一次没人会记得。
    """
    handlers = _broad_turn_handlers()
    assert handlers, "一处都没找到，说明这条测试的判据本身失效了（不是代码变干净了）"

    offenders = [h.lineno for h in handlers if _sends_internal_error(h)]
    assert not offenders, (
        f"ws.py 第 {offenders} 行的回合失败仍在直接发 INTERNAL_ERROR，"
        "额度用完会被报成服务器出错，玩家会照着提示无限重试。"
        "改用 `_send_turn_failure(websocket, exc)`。"
    )


def test_the_quota_message_does_not_tell_the_player_to_try_again() -> None:
    """额度用完时不许再补那句「请用一句更明确的行动再说一次」。

    它是给"守秘人卡住了"准备的兜底旁白，对"今天用完了"是最坏的建议。
    """
    source = _WS.read_text(encoding="utf-8")
    assert "if not isinstance(exc, QuotaExceeded):" in source, "兜底旁白没有把额度用完这条路排除掉"
