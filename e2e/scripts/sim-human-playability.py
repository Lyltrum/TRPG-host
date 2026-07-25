#!/usr/bin/env python3
"""模拟真人主路径：五预设模组 建房→前情→建卡→game.start 开场→首动回应。

在 trpg-backend 目录执行：
  .venv/bin/python ../e2e/scripts/sim-human-playability.py

依赖 backend 的 TestClient + 本地 .env（DeepSeek）。产物：
  e2e/artifacts/sim-human-playability.json
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

# 允许从仓库根/ e2e 调用时找到 app
BACKEND = Path(__file__).resolve().parents[2] / "trpg-backend"
sys.path.insert(0, str(BACKEND))

from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

ROOMS = "/api/v1/rooms"
OUT = Path(__file__).resolve().parents[1] / "artifacts" / "sim-human-playability.json"


def reg(client: TestClient, acc: str) -> str:
    r = client.post(
        "/api/v1/auth/register",
        json={"account": acc, "password": "secret1", "nickname": "模拟玩家"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def play_module(client: TestClient, mod: dict) -> dict:
    mid, title = mod["id"], mod["title"]
    token = reg(client, f"sim_{uuid.uuid4().hex[:10]}")
    auth = {"Authorization": f"Bearer {token}"}
    r = client.post(
        ROOMS,
        json={"roomName": f"sim-{title}", "nickname": "模拟玩家", "maxPlayers": 1},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    room = r.json()["data"]
    rid, rtok, pid = room["roomId"], room["reconnectToken"], room["playerId"]
    rh = {"X-Reconnect-Token": rtok}

    d = client.get(f"/api/v1/modules/{mid}", headers=auth).json()["data"]
    pages = d.get("storyPages") or []
    intro_ok = bool(pages) and not any("DeepSeek" in x for x in pages)

    assert (
        client.post(
            f"{ROOMS}/{rid}/module",
            json={"moduleId": mid, "attributeGenMethod": "point_buy"},
            headers=rh,
        ).status_code
        == 200
    )
    assert client.post(f"{ROOMS}/{rid}/start-story", headers=rh).status_code == 200
    cid = client.post(f"{ROOMS}/{rid}/characters", headers=rh).json()["data"]["characterId"]
    client.patch(
        f"{ROOMS}/{rid}/characters/{cid}",
        json={
            "name": "模拟探员",
            "attributes": {
                k: 50 for k in ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU", "LUCK"]
            },
            "derivedStats": {"HP": 10, "SAN": 50, "MP": 10},
            "skills": {},
            "equipment": [],
            "occupation": None,
            "background": "",
            "notes": "",
        },
        headers=rh,
    )
    cr = client.post(f"{ROOMS}/{rid}/characters/{cid}/complete", headers=rh)
    assert cr.status_code == 200, cr.text

    opening = narration = action_echo = None
    errors: list[str] = []
    check_pending = False

    with client.websocket_connect(f"/ws/{rid}?token={token}") as ws:
        ws.send_json(
            {
                "type": "room.join",
                "playerId": pid,
                "payload": {"reconnectToken": rtok},
            }
        )
        bound = ws.receive_json()
        if bound.get("type") != "session.bound":
            errors.append(f"bound {bound}")

        ws.send_json({"type": "game.start", "playerId": pid, "payload": {}})
        try:
            m = ws.receive_json()
            if m.get("type") == "narration.push":
                opening = m["payload"]["text"]
            elif m.get("type") == "error":
                errors.append(f"start {m['payload']}")
            else:
                errors.append(f"start unexpected {m}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"open {e}")

        replay = client.get(f"{ROOMS}/{rid}/replay", headers=rh).json()["data"]
        narr_events = [e for e in replay if e.get("eventType") == "narration.push"]
        replay_opening = (
            (narr_events[0].get("payload") or {}).get("text") if narr_events else None
        )

        ws.send_json(
            {
                "type": "action.submit",
                "playerId": pid,
                "payload": {"utterance": "我先仔细观察一下眼前的情况，有什么异常吗？"},
            }
        )
        deadline = time.time() + 180
        while time.time() < deadline and narration is None and not check_pending:
            try:
                m = ws.receive_json()
            except Exception as e:  # noqa: BLE001
                errors.append(f"act recv {e}")
                break
            t = m.get("type")
            if t == "action.broadcast":
                action_echo = m["payload"].get("utterance")
            elif t == "narration.push":
                text = m["payload"].get("text")
                if text and text != opening:
                    narration = text
            elif t in ("check.request", "san.check.request"):
                check_pending = True
                narration = narration or f"[check] {m['payload'].get('skill')}"
            elif t == "error":
                errors.append(f"act {m.get('payload')}")
                break

    stale = "案件已加载" in (opening or "")
    return {
        "title": title,
        "moduleId": mid,
        "intro_ok": intro_ok,
        "story_pages": len(pages),
        "opening_len": len(opening or ""),
        "opening_preview": (opening or "")[:80],
        "opening_not_stale": bool(opening) and not stale,
        "replay_has_opening": bool(replay_opening),
        "action_echo": bool(action_echo),
        "narration_len": len(narration or ""),
        "narration_preview": (narration or "")[:140],
        "check_pending": check_pending,
        "action_ok": bool(narration or check_pending),
        "errors": errors,
        "playable": bool(intro_ok and opening and not stale and (narration or check_pending)),
    }


def main() -> int:
    results: list[dict] = []
    with TestClient(app) as client:
        mods = client.get("/api/v1/modules").json()["data"]
        print("modules", len(mods), flush=True)
        from app.core.config import get_settings
        from app.core.narrator import build_narrator

        print("narrator", type(build_narrator(get_settings())).__name__, flush=True)
        for mod in mods:
            print(f"\n=== {mod['title']} ===", flush=True)
            try:
                r = play_module(client, mod)
            except Exception as e:  # noqa: BLE001
                r = {
                    "title": mod["title"],
                    "playable": False,
                    "errors": [f"{type(e).__name__}: {e}"],
                }
            results.append(r)
            print(json.dumps(r, ensure_ascii=False, indent=2), flush=True)

    print("\n======== SUMMARY ========")
    for r in results:
        print(
            ("PASS" if r.get("playable") else "FAIL"),
            r.get("title"),
            "open",
            r.get("opening_len"),
            "act",
            r.get("narration_len"),
            "err",
            r.get("errors"),
        )
    n = sum(1 for r in results if r.get("playable"))
    print(f"playable {n}/{len(results)}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
