#!/usr/bin/env python3
"""多人真机实测装置：两个真人玩家 + 一个 AI 队友，同房间双 WS。

## 🔴 这是**工具**，不是网

它连真后端、跑**真实大模型**（要 `DEEPSEEK_API_KEY`，一跑约 2–3 分钟），
所以结果不确定、不能进 CI。多人那张**确定性**的回归网在
`e2e/tests/multiplayer-split.e2e.ts`——那条摆好 `keeper_state` 之后只验投递。

两者分工：网守「改动有没有把投递弄坏」，这个脚本守「裁决器在真实模组上
到底会不会用对那些字段」——后者只有真模型答得了（`exec/33 §9` 七跑里，
`movers` 用没用对、分头成不成立，全靠它）。

## 用法

    cd trpg-backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
    .venv/bin/python ../e2e/scripts/mp-playtest.py [module_id]

## 🔴 判据要**跑之前**写死

见 `exec/33 §9`。每次跑之前把这次要验的几条列出来，跑完逐条对账——不要跑完
再去数据里找解释。已经吃过的亏：一跑的"隔离成功"其实是**位置被清空**的副作用
凑出来的假成功，没有第二跑就会一直被当成已验收。

产出：每个客户端**各自收到了什么**（投递隔离要靠这个判），以及事件表/keeper_state。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections import defaultdict

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"
API = f"{BASE}/api/v1"

received: dict[str, list[tuple[float, dict]]] = defaultdict(list)
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


async def api(client, method, path, *, token=None, rtok=None, json_body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if rtok:
        headers["X-Reconnect-Token"] = rtok
    r = await client.request(method, f"{API}{path}", headers=headers, json=json_body)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    body = r.json()
    return body.get("data")


class Client:
    """一个玩家的 WS 连接。收到的消息全存下来，谁收到了什么是本次实测的核心数据。"""

    def __init__(self, name, player_id, rtok, token):
        self.name, self.player_id, self.rtok, self.token = name, player_id, rtok, token
        self.ws = None
        self.task = None

    async def connect(self, room_id):
        self.ws = await websockets.connect(
            f"{WS}/ws/{room_id}?token={self.token}", max_size=None, proxy=None
        )
        await self.send("room.join", {"reconnectToken": self.rtok})
        self.task = asyncio.create_task(self._pump())
        await asyncio.sleep(0.6)

    async def _pump(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                received[self.name].append((time.time() - T0, msg))
                kind = msg.get("type")
                if kind in ("check.request", "san.check.request"):
                    payload = msg.get("payload") or {}
                    # 🔴 只掷**自己**那一个：第一版两个客户端都去掷，第二个必然
                    # 拿到 CHECK_NOT_PENDING，噪声盖住真信号。
                    if payload.get("playerId") != self.player_id:
                        continue
                    # 服务端权威掷骰：玩家点一下就行
                    await asyncio.sleep(0.3)
                    roll = "san.check.roll" if kind.startswith("san") else "check.roll"
                    await self.send(roll, {"checkRequestId": payload.get("checkRequestId")})
        except Exception as exc:  # noqa: BLE001
            log(f"!! {self.name} pump 结束：{type(exc).__name__} {exc}")

    async def send(self, event_type, payload):
        await self.ws.send(
            json.dumps({"type": event_type, "playerId": self.player_id, "payload": payload})
        )

    async def confirm_merge(self):
        log(f"→ {self.name}：[点击] 已会合")
        await self.send("party.merge.confirm", {})

    def latest_party(self):
        """这个客户端最后一次收到的 party.update payload。"""
        for _t, m in reversed(received[self.name]):
            if m.get("type") == "party.update":
                return m.get("payload") or {}
        return None

    async def say(self, utterance, *, private=False):
        log(f"→ {self.name}：{utterance}")
        await self.send(
            "action.submit", {"utterance": utterance, "visibility": "private" if private else None}
        )

    async def close(self):
        if self.task:
            self.task.cancel()
        if self.ws:
            await self.ws.close()


def _count() -> int:
    return sum(len(v) for v in received.values())


async def wait_quiet(seconds=6.0, timeout=180.0):
    """先等**新**消息到达，再等它安静下来。

    🔴 第一版只看"距上一条消息多久"，于是一条都还没来时它立刻返回——八句话
    一次性全灌进去，那一轮的数据整份作废。装置自己的判据也要先验一遍
    （项目判据：验证器上线前先造一个必然通过和一个必然失败的样本）。
    """
    start = time.time()
    n0 = _count()
    while _count() == n0:
        if time.time() - start > timeout:
            log("!! 等不到任何回应")
            return False
        await asyncio.sleep(0.5)
    while time.time() - start < timeout:
        last = max(t for msgs in received.values() for t, _ in msgs)
        if time.time() - T0 - last > seconds:
            return True
        await asyncio.sleep(0.5)
    log("!! wait_quiet 超时")
    return True


def _busy(client) -> bool:
    """这个客户端看到的最后一条 keeper.busy。没收到过就当不忙。"""
    for _t, m in reversed(received[client.name]):
        if m.get("type") == "keeper.busy":
            return bool((m.get("payload") or {}).get("busy"))
    return False


async def wait_idle(*clients, quiet=5.0, timeout=120.0):
    """等到「没人在忙」且**安静**为止。

    只等安静是不够的：排队中的提交会让房间在安静之后又忙起来，那时再发言
    第二个人会被 QUEUED 排到下一轮——同一轮两组发言这个前提就不成立了。
    """
    start = time.time()
    while time.time() - start < timeout:
        last = max((t for msgs in received.values() for t, _ in msgs), default=0.0)
        if not any(_busy(c) for c in clients) and time.time() - T0 - last > quiet:
            return True
        await asyncio.sleep(0.5)
    log("!! wait_idle 超时，房间一直忙")
    return False


async def main():
    module_id = sys.argv[1] if len(sys.argv) > 1 else None
    # trust_env=False：沙箱里有 HTTP 代理，httpx 默认会走它 → 127.0.0.1 被拦成 403
    async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
        # 两个真人账号
        players = []
        for nick in ("阿福", "阿贵"):
            acc = f"mp_{uuid.uuid4().hex[:10]}"
            data = await api(
                http,
                "POST",
                "/auth/register",
                json_body={"account": acc, "password": "secret1", "nickname": nick},
            )
            players.append({"nickname": nick, "token": data["token"]})

        host, guest = players
        room = await api(
            http,
            "POST",
            "/rooms",
            token=host["token"],
            json_body={"roomName": "多人实测", "nickname": "阿福", "maxPlayers": 4},
        )
        room_id, code = room["roomId"], room["roomCode"]
        host |= {"playerId": room["playerId"], "rtok": room["reconnectToken"]}
        log(f"房间 {code} ({room_id[:8]})")

        joined = await api(
            http,
            "POST",
            f"/rooms/{code}/join",
            token=guest["token"],
            json_body={"nickname": "阿贵"},
        )
        guest |= {"playerId": joined["playerId"], "rtok": joined["reconnectToken"]}

        if module_id is None:
            mods = await api(http, "GET", "/modules", token=host["token"])
            module_id = (mods[0] if isinstance(mods, list) else mods["items"][0])["id"]
        await api(
            http,
            "POST",
            f"/rooms/{room_id}/module",
            rtok=host["rtok"],
            json_body={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        )
        ai = await api(http, "POST", f"/rooms/{room_id}/ai-players", rtok=host["rtok"])
        log(f"AI 队友：{ai['nickname']}")

        await api(http, "POST", f"/rooms/{room_id}/start-story", rtok=host["rtok"])
        for p in (host, guest):
            await api(
                http,
                "POST",
                f"/rooms/{room_id}/characters/quick-build",
                rtok=p["rtok"],
                json_body={"name": p["nickname"]},
            )
        log("两张一键生成的卡就绪")

    a = Client("阿福", host["playerId"], host["rtok"], host["token"])
    b = Client("阿贵", guest["playerId"], guest["rtok"], guest["token"])
    await a.connect(room_id)
    await b.connect(room_id)
    await b.send("player.ready", {"ready": True})
    await asyncio.sleep(1.0)
    await a.send("game.start", {})
    log("开局，等开场白…")
    await wait_quiet(8.0)

    # ── 剧本：每一句都对准一条已知嫌疑 ──────────────────
    script = [
        (a, "我们一起走到街对面，站在科比特家门口。", "基线：全队同处"),
        (a, "我一个人绕到房子后面去看看，阿贵你留在门口盯着。", "分头：位置要真的分开"),
        (b, "我在门口仔细听屋里的动静。", "隔离：这段阿福该看不见"),
    ]
    for who, line, why in script:
        log(f"—— {why}")
        await who.say(line)
        await wait_quiet(7.0)

    # 🔴 §4 的验收必须**两组在同一轮里都发言**——一人一轮只会产生一段，并行
    # 无从谈起。两句要落进同一个收集窗口，所以**必须先等房间真的闲下来**：
    # 上一版只 wait_quiet 就发，而排队中的提交会让房间持续忙着，第二个人当场
    # 被 QUEUED 排到下一轮 → 那一跑的 §4 数据整份作废。
    log("—— 等房间真的闲下来（keeper.busy 落回 False 且不再有新消息）")
    await wait_idle(a, b)
    log("—— 并行：同一轮两组各说各的（§4 / §3.2 验收）")
    await asyncio.gather(
        a.say("我蹲下来仔细搜查屋后的地面，找找有没有留下什么痕迹。"),
        b.say("我也在门口地上仔细找找，看有没有脚印。"),
    )
    await wait_quiet(8.0)

    log("—— 会合：该挂起等确认")
    await b.say("我绕过去找阿福，走到房子后面。")
    await wait_quiet(7.0)

    log("—— 会合确认：点之前先看一眼是不是还分着组")
    before_confirm = {"阿福": a.latest_party(), "阿贵": b.latest_party()}
    await b.confirm_merge()
    await wait_quiet(4.0, timeout=30.0)
    after_confirm = {"阿福": a.latest_party(), "阿贵": b.latest_party()}

    await a.close()
    await b.close()

    out = {
        "room_id": room_id,
        "players": {"阿福": host["playerId"], "阿贵": guest["playerId"], "AI": ai["playerId"]},
        "before_confirm": before_confirm,
        "after_confirm": after_confirm,
        "received": {
            name: [
                (round(t, 1), m.get("type"), m.get("payload"))
                for t, m in msgs
                if m.get("type") not in ("session.bound", "room.state")
            ]
            for name, msgs in received.items()
        },
    }
    path = os.environ.get("MP_RESULT", "/tmp/mp_result.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"写入 {path}")
    for name, msgs in received.items():
        kinds = defaultdict(int)
        for _, m in msgs:
            kinds[m.get("type")] += 1
        log(f"{name} 收到：{dict(kinds)}")


asyncio.run(main())
