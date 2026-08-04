#!/usr/bin/env python3
"""模组试跑：让一个自主驱动器把模组从开场玩到结局（`exec/29` 判据 ③「走得通」）。

## 它回答什么

`validate` 是静态检查，抓不到「结构完全合法，但那条关键线索被放进了错误的房间，
于是玩家永远走不到结局」。**这类失败只有真的玩一遍才会现形。**

判据是**排除性**的：**跑不通几乎一定是模组坏了；跑通不代表模组好。**
这个不对称正是我们要的——它是个便宜的排除器，不是质量分。

## 🔴 「跑不通」有两种，第一版把它们混成了同一个 bool

实测林中屋：80 轮撞满、判「没走到结局」。而真相是**这个模组根本没有可到达的
结局**——它的 `endings[]` 只有一条 `epilogue`，源头是原文里的**一行**，取文层
自己都写着「关于模组尾声的叙述，提供战役延续的可能性」。收束的唯一入口是
`ending_reached`（`progression/executor.py`），所以对这类模组，
`对局阶段==finished` **结构上不可能被满足**：不管模组转得多好、守秘人主持得
多好，判据都返回 False。

这跟第一版把判据写成代码库里不存在的字符串 `rooms.phase == "Finished"` 是**同一
类错**，只是升了一层——那次是常量错，这次是判据预设了一个模组不具备的属性。
当时给自己立的规矩是「验证器上线前先造一个必然通过和一个必然失败的样本，两头
都验」，而我只验了必然通过那头（追书人有 4 条真结局）。

所以结论不再是 bool，是 `verdict` 四选一，见 `_decide_verdict`。

## 🔴 这不是给 AI 队友恢复行动权

`exec/25 #60` 把 AI 队友的行动权**结构性地**拿掉了：它的话发进讨论区，那条通道
不写 events 表、不进任何 LLM 上下文，所以够不到裁决器。那条设计**没有被撤销**。

它的理由是「AI 会替真人做决定、把玩家的提问变成一次行动」——**而那个理由的前提
是桌上有真人**。试跑局里没有真人要保护，所以这里的驱动器可以有行动权。
但它必须是**另一个东西**（本文件），不能是把 `AiActor` 的通道改回去；否则下一个
人会以为 #60 被推翻了。

## 🔴 它同样走有限视角

驱动器只看**它自己收到的叙事**，拿不到 structured、拿不到线索账本。理由跟
AI 玩家一样：给它看剧本，它会直奔结局，那样试跑就永远是绿的、什么都验不出来。

## 用法

    cd trpg-backend && .venv/bin/python ../e2e/scripts/module-playthrough.py \\
        --module 00000000-0000-0000-0000-000000000003 --max-turns 30

环境变量 `PLAYTHROUGH_MAX_TURNS` 同 `--max-turns`。
产物：`e2e/artifacts/module-playthrough-<模组后缀>.json`（**gitignored**，含真实叙事）。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "trpg-backend"
sys.path.insert(0, str(BACKEND))

from openai import OpenAI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
ROOMS = "/api/v1/rooms"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

DRIVER_SYSTEM = """\
你在扮演一名克苏鲁的呼唤（COC7）调查员，正在线上跑团。守秘人由 AI 担任。

你的任务：**推进调查，直到故事收束**。

规则：
- 每次只输出**一句**你要做的事，用第一人称，像真人在桌上说话。
- 说**具体行动**（去哪、看什么、问谁、拿什么），不要说"我该做什么"这类空转。
- 不要重复上一轮已经做过的事；如果那里查完了，换个地方或换个对象。
- 你**没有看过剧本**，只知道守秘人告诉过你的东西。凭这些推理。
- 不要替守秘人描述结果，只说你的行动。

只输出那一句话本身，不要引号、不要解释。"""


def _recv_json(ws, timeout: float = 60.0) -> dict:
    """`receive_json` 会无限阻塞；daemon 线程 + join 超时（沿用质检脚本的做法）。"""
    box: queue.Queue = queue.Queue(1)

    def _worker() -> None:
        try:
            box.put(ws.receive_json())
        except Exception as exc:  # noqa: BLE001
            box.put(exc)

    threading.Thread(target=_worker, daemon=True).start()
    try:
        val = box.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("等待服务端消息超时") from exc
    if isinstance(val, Exception):
        raise val
    return val


@dataclass
class TurnRecord:
    turn: int
    utterance: str
    kp_chars: int
    checks: list[str] = field(default_factory=list)


#: 一轮结束却一条叙事都没收到——对局卡住了，跟"模组没结局"是两回事。
NO_REPLY = "本轮未收到任何叙事"

VERDICTS = {
    "ending": "到达结局",
    "open-ended": "模组没有可到达的结局，跑满且无死锁",
    "stalled": "模组有结局但没走到",
    "broken": "对局卡住或驱动器空转",
}


@dataclass
class PlaythroughResult:
    module_id: str
    module_title: str
    #: `VERDICTS` 的键。**不是 bool**——见模块文档「跑不通有两种」。
    verdict: str = ""
    verdict_reason: str = ""
    ending_id: str | None = None
    #: 模组自己提供了哪些结局 id。空 = 这个模组压根没有可到达的收束点。
    module_ending_ids: list[str] = field(default_factory=list)
    turns_used: int = 0
    max_turns: int = 0
    #: 🔴 **终局快照，不是访问历史。** 旧名字叫 `nodes_visited`，我因此拿它当
    #: 走过的节点列表读，得出「`当前场景节点` 整局没被写过」——而事件表里
    #: `keeper.node` 写了 74 次、落在 9 个真实节点上。名字撒的谎比空值更贵：
    #: 空值会让人去查，错名字会让人直接下结论。走过哪些节点要读 events 表。
    final_state: list[str] = field(default_factory=list)
    llm_calls: int = 0
    elapsed_s: float = 0.0
    turns: list[TurnRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _load_api_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY（读的是 trpg-backend/.env）")
    return key


def _register(client: TestClient) -> str:
    r = client.post(
        "/api/v1/auth/register",
        json={
            "account": f"pt_{uuid.uuid4().hex[:10]}",
            "password": "secret1",
            "nickname": "试跑员",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def _complete_character(client: TestClient, room_id: str, rtok: str) -> None:
    """建一张属性平庸的合法卡——试跑要验的是模组，不是角色强度。"""
    rh = {"X-Reconnect-Token": rtok}
    cid = client.post(f"{ROOMS}/{room_id}/characters", headers=rh).json()["data"]["characterId"]
    client.patch(
        f"{ROOMS}/{room_id}/characters/{cid}",
        json={
            "name": "试跑调查员",
            "attributes": dict.fromkeys(
                ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU", "LUCK"], 55
            ),
            "derivedStats": {"HP": 11, "SAN": 55, "MP": 11},
            "skills": {},
            "equipment": [],
            "occupation": None,
            "background": "",
            "notes": "",
        },
        headers=rh,
    )
    assert (
        client.post(f"{ROOMS}/{room_id}/characters/{cid}/complete", headers=rh).status_code == 200
    )


def _keeper_state(room_id: str) -> dict:
    """读 `keeper_state`。

    🔴 **结局判据只能看这里，不能看 `rooms.phase`。** 系统里有两个"阶段"：
    `rooms.phase` 是房间生命周期（Lobby/Building/InGame/Completed），
    `keeper_state["对局阶段"]` 才是对局阶段（opening/investigation/finished）。
    `set_phase_impl` 收束时**只写后者**，`rooms.phase` 一动不动。

    第一版我写成了 `rooms.phase == "Finished"` —— 那个字符串在代码库里根本不存在，
    于是判据永远返回 False：**不管模组好坏都报「没走到结局」，验证器自己是坏的，
    而且坏得毫无声息。** 在花钱跑完整局之前抓到的。
    """
    import asyncio

    from sqlalchemy import select

    from app.core.db import async_session_factory
    from app.models.room import Room

    box: dict = {}

    async def _read() -> None:
        async with async_session_factory() as db:
            room = (await db.scalars(select(Room).where(Room.id == room_id))).first()
            box.update(dict(room.keeper_state or {}) if room is not None else {})

    try:
        asyncio.run(_read())
    except Exception:  # noqa: BLE001 — 读不到就当没收束，下一轮再看
        return {}
    return box


def _is_finished(state: dict) -> bool:
    return str(state.get("对局阶段") or "") == "finished"


def _module_ending_ids(module_id: str) -> list[str]:
    """这个模组提供了哪些可到达的结局 id。

    🔴 **验证器读剧本是合法的，驱动器读剧本才不合法。** 有限视角约束的是"演员
    看得见什么"（给它剧本它会直奔结局），不是"裁判按什么判"。裁判必须知道
    及格线在哪，否则就是拿一把没有刻度的尺子量。

    🔴 解析不出来直接炸，不返回空列表——空列表的语义是「这个模组没有结局」，
    而"读不到"跟"没有"是两件事。混成同一个值正是这次要修的那个 bug 的同族。
    """
    import asyncio

    from app.core.config import get_settings
    from app.core.db import async_session_factory
    from app.core.keeper.contract.catalog import default_modules_dir
    from app.core.keeper.contract.source import resolve_module

    settings = get_settings()
    modules_dir = (
        Path(settings.keeper_modules_dir).expanduser().resolve()
        if settings.keeper_modules_dir
        else default_modules_dir()
    )

    box: list[str] = []

    async def _read() -> None:
        async with async_session_factory() as db:
            resolved = await resolve_module(db, modules_dir, module_id)
            if resolved is None:
                raise SystemExit(f"解析不出剧本，无法定判据：{module_id}")
            box.extend(e.id for e in resolved.module.endings)

    asyncio.run(_read())
    return box


def _decide_verdict(
    *,
    finished: bool,
    module_ending_ids: list[str],
    silent_turns: int,
    turns_used: int,
    max_turns: int,
    driver_stalled: bool,
) -> tuple[str, str]:
    """判据本体。返回 (verdict, 一句话理由)。

    顺序有语义：**先判卡住，再判有没有及格线。** 一局既卡住又没结局时，该报的
    是卡住——「模组没有结局」是个良性事实，用它盖掉死锁就等于把 bug 洗白了。
    """
    if finished:
        return "ending", "到达结局"
    if driver_stalled:
        return "broken", "驱动器没给出行动"
    if silent_turns:
        return "broken", f"{silent_turns} 轮没有收到任何叙事——对局卡住了"
    if not module_ending_ids:
        return "open-ended", f"模组没有可到达的结局；跑满 {turns_used} 轮无死锁"
    return "stalled", f"{max_turns} 轮内未收束（模组有 {len(module_ending_ids)} 条结局）"


def _decide(oa: OpenAI, history: list[str], result: PlaythroughResult) -> str:
    """驱动器这一轮说什么。只喂它**自己收到过的**叙事，不喂剧本。"""
    convo = "\n\n".join(history[-12:])
    resp = oa.chat.completions.create(
        model=DEEPSEEK_MODEL,
        temperature=0.8,
        max_tokens=120,
        messages=[
            {"role": "system", "content": DRIVER_SYSTEM},
            {"role": "user", "content": f"到目前为止发生的事：\n\n{convo}\n\n你这一轮做什么？"},
        ],
    )
    result.llm_calls += 1
    return (resp.choices[0].message.content or "").strip().strip("「」\"'")


def _drain_until_idle(ws, result: PlaythroughResult, history: list[str], turn: int) -> list[str]:
    """收完这一轮服务端要说的话；遇到待掷检定就掷。返回本轮触发的检定技能名。

    🔴 **一轮里可能有两条叙事，不能收到第一条就收工。** `exec/28` 把掷骰拆成了
    两拍：裁决发起检定 → 广播 `check.request` → 玩家掷 → `check.result` →
    **然后**才是结算叙事。第一版把「收到 narration.push」当本轮结束，于是结算
    叙事被漏掉、在下一轮开头被当成下一轮的回复——驱动器看到的历史整体错位，
    最后原地打转（实测追书人 30 轮里 10 轮没拿到回复、末 5 轮重复同一句）。

    正确的终止条件是「**收到叙事，且没有待结算的检定**」。
    同族判据：改了时序就要回头查依赖旧时序的代码（`exec/23 #58`）。

    🔴 掷骰要按 `checkRequestId` 去重：同一条 `check.request` 可能被重复推送
    （重连补发），重复提交会拿到 `CHECK_NOT_PENDING`。
    """
    checks: list[str] = []
    kp_text: list[str] = []
    rolled: set[str] = set()
    pending = 0
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            msg = _recv_json(ws, timeout=min(90.0, max(5.0, deadline - time.time())))
        except TimeoutError:
            break
        mtype = msg.get("type")
        payload = msg.get("payload") or {}

        if mtype in ("check.request", "san.check.request"):
            crid = str(payload.get("checkRequestId") or "")
            if crid and crid in rolled:
                continue
            rolled.add(crid)
            checks.append(payload.get("skill") or mtype)
            pending += 1
            ws.send_json(
                {
                    "type": "check.roll" if mtype == "check.request" else "san.check.roll",
                    "payload": {"checkRequestId": crid},
                }
            )
            continue

        if mtype in ("check.result", "san.check.result"):
            pending = max(0, pending - 1)
            continue

        if mtype == "narration.push":
            kp_text.append(payload.get("text") or "")
            if pending == 0:
                break
            continue

        if mtype == "error":
            code = str(payload.get("code") or "")
            result.errors.append(f"turn{turn} {payload}")
            if code == "CHECK_NOT_PENDING":
                pending = max(0, pending - 1)
                continue
            break

    if kp_text:
        history.append("守秘人：" + " ".join(kp_text))
    else:
        history.append("守秘人：（没有回应）")
        result.errors.append(f"turn{turn} {NO_REPLY}")
    return checks


def run(module_id: str, max_turns: int) -> PlaythroughResult:
    oa = OpenAI(api_key=_load_api_key(), base_url=DEEPSEEK_BASE_URL, timeout=180.0)
    result = PlaythroughResult(module_id=module_id, module_title="", max_turns=max_turns)
    t0 = time.perf_counter()

    with TestClient(app) as client:
        mods = client.get("/api/v1/modules").json()["data"]
        mod = next((m for m in mods if m["id"] == module_id), None)
        if mod is None:
            raise SystemExit(f"模组 id 不在目录里：{module_id}")
        result.module_title = mod["title"]
        result.module_ending_ids = _module_ending_ids(module_id)

        token = _register(client)
        auth = {"Authorization": f"Bearer {token}"}
        room = client.post(
            ROOMS,
            json={"roomName": f"试跑-{mod['title']}", "nickname": "试跑员", "maxPlayers": 1},
            headers=auth,
        ).json()["data"]
        rid, rtok, pid, _rcode = (
            room["roomId"],
            room["reconnectToken"],
            room["playerId"],
            room["roomCode"],
        )
        rh = {"X-Reconnect-Token": rtok}
        client.post(
            f"{ROOMS}/{rid}/module",
            json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
            headers=rh,
        )
        client.post(f"{ROOMS}/{rid}/start-story", headers=rh)
        _complete_character(client, rid, rtok)

        history: list[str] = []
        driver_stalled = False
        with client.websocket_connect(f"/ws/{rid}?token={token}") as ws:
            ws.send_json(
                {"type": "room.join", "playerId": pid, "payload": {"reconnectToken": rtok}}
            )
            _recv_json(ws, timeout=30)

            ws.send_json({"type": "game.start", "playerId": pid, "payload": {}})
            _drain_until_idle(ws, result, history, 0)

            for turn in range(1, max_turns + 1):
                state = _keeper_state(rid)
                if _is_finished(state):
                    result.ending_id = str(state.get("结局") or "") or None
                    break
                utter = _decide(oa, history, result)
                if not utter:
                    driver_stalled = True
                    break
                history.append("我：" + utter)
                ws.send_json(
                    {"type": "action.submit", "playerId": pid, "payload": {"utterance": utter}}
                )
                checks = _drain_until_idle(ws, result, history, turn)
                result.turns_used = turn
                result.turns.append(
                    TurnRecord(
                        turn=turn,
                        utterance=utter,
                        kp_chars=len(history[-1]) if history else 0,
                        checks=checks,
                    )
                )
                print(f"  第 {turn:2d} 轮 · {utter[:32]}… · 检定 {checks}", flush=True)

        final_state = _keeper_state(rid)
        finished = _is_finished(final_state)
        if finished and not result.ending_id:
            result.ending_id = str(final_state.get("结局") or "") or None
        result.verdict, result.verdict_reason = _decide_verdict(
            finished=finished,
            module_ending_ids=result.module_ending_ids,
            silent_turns=sum(1 for e in result.errors if NO_REPLY in e),
            turns_used=result.turns_used,
            max_turns=max_turns,
            driver_stalled=driver_stalled,
        )
        # 停在哪：场景指针是代码写的 id，不是模型现编的自由文本
        for key in ("当前场景节点", "当前场景", "对局阶段"):
            if final_state.get(key):
                result.final_state.append(f"{key}={final_state[key]}")

    result.elapsed_s = round(time.perf_counter() - t0, 1)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="模组试跑：能不能从开场玩到结局")
    ap.add_argument("--module", required=True, help="scenario id")
    ap.add_argument(
        "--max-turns",
        type=int,
        default=int(os.environ.get("PLAYTHROUGH_MAX_TURNS", "30")),
    )
    args = ap.parse_args()

    result = run(args.module, args.max_turns)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"module-playthrough-{args.module[-4:]}.json"
    out.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"模组       {result.module_title}（结局 {len(result.module_ending_ids)} 条）")
    print(f"判定       {result.verdict} · {VERDICTS[result.verdict]}  {result.ending_id or ''}")
    print(f"理由       {result.verdict_reason}")
    print(f"用了轮数   {result.turns_used} / {result.max_turns}")
    print(f"驱动器调用 {result.llm_calls} 次 | 总耗时 {result.elapsed_s}s")
    if result.errors:
        print(f"错误       {result.errors[:3]}")
    print(f"报告       {out}")


if __name__ == "__main__":
    main()
