#!/usr/bin/env python3
"""多会话 × 多模组 × 多轮：压测 AI 守秘人是否像真人 KP。

在 trpg-backend 目录或仓库根执行：
  cd trpg-backend && .venv/bin/python ../e2e/scripts/kp-quality-stress.py

环境变量：
  KP_SESSIONS_PER_MODULE  每模组会话数（默认 4）
  KP_TURNS                每会话轮数（默认 12）
  KP_MODULES              逗号分隔模组 id 后缀 003,004,... 或 all（默认 all）

产物：e2e/artifacts/kp-quality-report.json + .md
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "trpg-backend"
sys.path.insert(0, str(BACKEND))

from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
ROOMS = "/api/v1/rooms"


def _recv_json(ws, timeout: float = 45.0) -> dict:
    """receive_json 会无限阻塞；daemon 线程 + join 超时，避免 ThreadPool shutdown 再卡死。"""
    box: queue.Queue = queue.Queue(1)

    def _run() -> None:
        try:
            box.put((True, ws.receive_json()))
        except Exception as e:  # noqa: BLE001 — 原样抛回主线程
            box.put((False, e))

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=timeout)
    if th.is_alive():
        raise TimeoutError(f"ws.receive_json timed out after {timeout}s")
    ok, val = box.get_nowait()
    if not ok:
        raise val  # type: ignore[misc]
    return val  # type: ignore[return-value]

# ── 玩家话术库：覆盖行动/迷茫/对话/危险意图（全模组通用，非单模组硬编码）──
UTTERANCES = [
    "我想去现场仔细看一看。",
    "我靠近最可疑的地方，看看有没有异常。",
    "我敲门/设法引起里面人的注意。",
    "我试图进入建筑内部调查。",
    "我搜索附近有没有掉落物或痕迹。",
    "我竖起耳朵仔细听周围的声音。",
    "我跟遇到的人搭话，打听最近发生了什么。",
    "我去找报纸或公开记录查背景。",
    "我躲在暗处观察一段时间。",
    "我检查门锁和窗户是否能打开。",
    "我顺着声音/灯光的方向走过去。",
    "我打开手电照亮眼前的区域。",
    "我该做什么？有点没头绪。",
    "接下来最优先该查什么？",
    "我想强行破门进去。",
    "我把刚才看到的记在本子上，再核对一遍细节。",
    "我退到安全距离，重新规划怎么接近。",
    "我呼叫有没有人，看看有没有回应。",
]


@dataclass
class Finding:
    code: str
    severity: str  # high | med | low
    module: str
    session: str
    turn: int
    player: str
    kp: str
    note: str


@dataclass
class TurnRecord:
    turn: int
    player: str
    kp: str
    flags: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


@dataclass
class SessionResult:
    module_title: str
    module_id: str
    session_id: str
    turns: list[TurnRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    opening: str = ""


# ── 质量规则：像不像真人 KP（通用）──
def analyze_kp(player: str, kp: str, *, is_opening: bool = False) -> list[tuple[str, str, str]]:
    """返回 (code, severity, note) 列表。"""
    flags: list[tuple[str, str, str]] = []
    if not kp or not kp.strip():
        flags.append(("empty_reply", "high", "空回复"))
        return flags

    text = kp.strip()
    n = len(text)

    # 过长（开场允许更长）
    limit = 320 if is_opening else 200
    if n > limit:
        flags.append(("too_long", "med", f"字数 {n} > {limit}"))

    # 虚拟挡行动：玩家明确行动时仍用「如果你/你可以」
    actiony = bool(
        re.search(
            r"我想|我要|我去|我打算|我决定|我尝试|我走进|我穿过|我敲|我搜索|我观察|我靠近|我进入",
            player,
        )
    )
    if actiony and re.search(r"如果你|你可以|要是你|倘若你|假如你", text):
        flags.append(
            (
                "virtual_block",
                "high",
                "玩家已宣告行动，KP 用「如果你/你可以」虚拟语气挡推进",
            )
        )

    # 软拒绝不推进：仍站在原地描写
    if actiony and re.search(r"你还站在|你仍站在|你站在自家|你还在门口犹豫", text):
        flags.append(("stuck_in_place", "high", "行动意图后仍把玩家钉在原地"))

    # 开场/街景复读：大段外观罗列（启发式）
    scenery_hits = len(
        re.findall(
            r"砖房|草坪|街灯|窗帘|煤油|月光|潮湿|气味|风吹|远处|车道|门廊|温室",
            text,
        )
    )
    if actiony and scenery_hits >= 4 and n > 120:
        flags.append(("scenery_dump", "high", f"行动轮景物堆砌 dens={scenery_hits}"))

    # 菜单感
    if re.search(r"你可以[：:]|你可以选择|选项[：:]|A[\.．]|B[\.．]", text):
        flags.append(("menu_options", "med", "出现选项菜单感"))

    # 幕后词泄漏
    if re.search(r"模组|剧本|裁决|narration|keeper_state|opening_complete", text):
        flags.append(("meta_leak", "high", "泄漏幕后术语"))

    # 替玩家决定下一步过多说教
    if re.search(r"你应该|你必须先|建议你先|作为调查员你该", text):
        flags.append(("preachy", "low", "说教/替玩家规划"))

    # 检定相关却编造骰点
    if re.search(r"掷出了?\s*\d+|骰子停在|你掷出", text) and "🎲" not in text:
        flags.append(("fake_dice", "high", "疑似在正文编造骰值"))

    # 迷茫问题却纯写景
    confused = bool(re.search(r"我该做|没头绪|怎么办|接下来", player))
    if confused and scenery_hits >= 3 and not re.search(r"可以|或许|也许|不妨|线索", text):
        flags.append(("confused_scenery", "high", "迷茫提问却只写景"))

    # 过短无信息
    if n < 15 and not is_opening:
        flags.append(("too_short", "med", f"过短 {n} 字"))

    return flags


def reg(client: TestClient, acc: str) -> str:
    r = client.post(
        "/api/v1/auth/register",
        json={"account": acc, "password": "secret1", "nickname": "压测员"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def complete_char(client: TestClient, room_id: str, rtok: str) -> None:
    rh = {"X-Reconnect-Token": rtok}
    cid = client.post(f"{ROOMS}/{room_id}/characters", headers=rh).json()["data"]["characterId"]
    client.patch(
        f"{ROOMS}/{room_id}/characters/{cid}",
        json={
            "name": "压测调查员",
            "attributes": {
                k: 55 for k in ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU", "LUCK"]
            },
            "derivedStats": {"HP": 11, "SAN": 55, "MP": 11},
            "skills": {},
            "equipment": [],
            "occupation": None,
            "background": "",
            "notes": "",
        },
        headers=rh,
    )
    assert client.post(f"{ROOMS}/{room_id}/characters/{cid}/complete", headers=rh).status_code == 200


def run_session(
    client: TestClient,
    mod: dict,
    session_idx: int,
    turns: int,
) -> SessionResult:
    mid, title = mod["id"], mod["title"]
    sid = f"{title[:4]}-{session_idx}-{uuid.uuid4().hex[:6]}"
    result = SessionResult(module_title=title, module_id=mid, session_id=sid)

    try:
        token = reg(client, f"kps_{uuid.uuid4().hex[:10]}")
        auth = {"Authorization": f"Bearer {token}"}
        room = client.post(
            ROOMS,
            json={"roomName": f"质检-{title}-{session_idx}", "nickname": "压测员", "maxPlayers": 1},
            headers=auth,
        ).json()["data"]
        rid, rtok, pid = room["roomId"], room["reconnectToken"], room["playerId"]
        rh = {"X-Reconnect-Token": rtok}

        assert (
            client.post(
                f"{ROOMS}/{rid}/module",
                json={"moduleId": mid, "attributeGenMethod": "point_buy"},
                headers=rh,
            ).status_code
            == 200
        )
        assert client.post(f"{ROOMS}/{rid}/start-story", headers=rh).status_code == 200
        complete_char(client, rid, rtok)

        with client.websocket_connect(f"/ws/{rid}?token={token}") as ws:
            ws.send_json(
                {
                    "type": "room.join",
                    "playerId": pid,
                    "payload": {"reconnectToken": rtok},
                }
            )
            bound = _recv_json(ws, timeout=30)
            if bound.get("type") != "session.bound":
                result.errors.append(f"bound={bound}")

            ws.send_json({"type": "game.start", "playerId": pid, "payload": {}})
            opening = ""
            t0 = time.time()
            while time.time() - t0 < 90:
                try:
                    m = _recv_json(ws, timeout=min(45.0, max(5.0, 90 - (time.time() - t0))))
                except Exception as e:
                    result.errors.append(f"open wait {e}")
                    break
                if m.get("type") == "narration.push":
                    opening = m["payload"]["text"]
                    break
                if m.get("type") == "error":
                    result.errors.append(f"start err {m.get('payload')}")
                    break
            result.opening = opening
            open_flags = analyze_kp("（开场）", opening, is_opening=True)
            result.turns.append(
                TurnRecord(
                    turn=0,
                    player="（开场）",
                    kp=opening,
                    flags=[f[0] for f in open_flags],
                )
            )

            for turn in range(1, turns + 1):
                utter = UTTERANCES[(turn - 1 + session_idx * 3) % len(UTTERANCES)]
                ws.send_json(
                    {
                        "type": "action.submit",
                        "playerId": pid,
                        "payload": {"utterance": utter},
                    }
                )
                kp_text = ""
                check_skills: list[str] = []
                deadline = time.time() + 120
                while time.time() < deadline:
                    try:
                        m = _recv_json(
                            ws, timeout=min(45.0, max(5.0, deadline - time.time()))
                        )
                    except Exception as e:
                        result.errors.append(f"t{turn} recv {e}")
                        break
                    t = m.get("type")
                    if t == "action.broadcast":
                        continue
                    if t == "narration.push":
                        text = m["payload"].get("text") or ""
                        if text != opening:
                            kp_text = text
                            break
                    if t in ("check.request", "san.check.request"):
                        skill = (m.get("payload") or {}).get("skill") or "SAN"
                        check_skills.append(str(skill))
                        cid = (m.get("payload") or {}).get("checkRequestId")
                        if cid:
                            roll_type = (
                                "san.check.roll" if t == "san.check.request" else "check.roll"
                            )
                            ws.send_json(
                                {
                                    "type": roll_type,
                                    "playerId": pid,
                                    "payload": {"checkRequestId": cid},
                                }
                            )
                    if t == "error":
                        code = (m.get("payload") or {}).get("code")
                        result.errors.append(f"t{turn} err {m.get('payload')}")
                        if code in ("CHECK_NOT_PENDING", "ACTION_IN_PROGRESS"):
                            continue
                        break
                # 若只有 check 还没正文，再等结算叙事
                if not kp_text and check_skills:
                    t1 = time.time() + 90
                    while time.time() < t1:
                        try:
                            m = _recv_json(
                                ws, timeout=min(45.0, max(5.0, t1 - time.time()))
                            )
                        except Exception:
                            break
                        if m.get("type") == "narration.push":
                            kp_text = m["payload"].get("text") or ""
                            break
                        if m.get("type") in ("check.request", "san.check.request"):
                            cid = (m.get("payload") or {}).get("checkRequestId")
                            skill = (m.get("payload") or {}).get("skill") or "SAN"
                            check_skills.append(str(skill))
                            if cid:
                                roll_type = (
                                    "san.check.roll"
                                    if m["type"] == "san.check.request"
                                    else "check.roll"
                                )
                                ws.send_json(
                                    {
                                        "type": roll_type,
                                        "playerId": pid,
                                        "payload": {"checkRequestId": cid},
                                    }
                                )

                flags = analyze_kp(utter, kp_text)
                result.turns.append(
                    TurnRecord(
                        turn=turn,
                        player=utter,
                        kp=kp_text,
                        flags=[f[0] for f in flags],
                        checks=check_skills,
                    )
                )
    except Exception as e:
        result.errors.append(f"{type(e).__name__}: {e}")

    return result


def main() -> int:
    sessions_per = int(__import__("os").environ.get("KP_SESSIONS_PER_MODULE", "4"))
    turns = int(__import__("os").environ.get("KP_TURNS", "12"))
    mod_filter = __import__("os").environ.get("KP_MODULES", "all")

    all_findings: list[Finding] = []
    sessions: list[SessionResult] = []

    with TestClient(app) as client:
        from app.core.config import get_settings
        from app.core.narrator import build_narrator

        s = get_settings()
        print("narrator", type(build_narrator(s)).__name__, "key", bool(s.deepseek_api_key))
        mods = client.get("/api/v1/modules").json()["data"]
        if mod_filter != "all":
            allow = {x.strip() for x in mod_filter.split(",") if x.strip()}
            mods = [m for m in mods if any(m["id"].endswith(a.zfill(3) if a.isdigit() else a) for a in allow) or m["id"] in allow or m["title"] in allow]

        print(f"modules={len(mods)} sessions_per={sessions_per} turns={turns}", flush=True)

        for mod in mods:
            for si in range(sessions_per):
                print(f"\n>>> {mod['title']} session {si + 1}/{sessions_per}", flush=True)
                sr = run_session(client, mod, si, turns)
                sessions.append(sr)
                # 展开 findings
                for tr in sr.turns:
                    for code in tr.flags:
                        sev = "med"
                        note = code
                        # re-analyze for severity
                        for c, s_, n in analyze_kp(
                            tr.player, tr.kp, is_opening=(tr.turn == 0)
                        ):
                            if c == code:
                                sev, note = s_, n
                                break
                        all_findings.append(
                            Finding(
                                code=code,
                                severity=sev,
                                module=sr.module_title,
                                session=sr.session_id,
                                turn=tr.turn,
                                player=tr.player[:80],
                                kp=tr.kp[:200],
                                note=note,
                            )
                        )
                print(
                    f"    turns={len(sr.turns)} flags={sum(len(t.flags) for t in sr.turns)} err={sr.errors}",
                    flush=True,
                )
                _write_quality_report(sessions, all_findings, partial=True)

    by_sev = _write_quality_report(sessions, all_findings, partial=False)
    total_turns = sum(len(s.turns) for s in sessions)
    return 0 if by_sev.get("high", 0) < total_turns * 0.5 else 1


def _write_quality_report(
    sessions: list[SessionResult],
    all_findings: list[Finding],
    *,
    partial: bool,
) -> Counter:
    by_code = Counter(f.code for f in all_findings)
    by_sev = Counter(f.severity for f in all_findings)
    by_mod = Counter(f.module for f in all_findings if f.severity == "high")
    total_turns = sum(len(s.turns) for s in sessions)
    high = [f for f in all_findings if f.severity == "high"]

    report = {
        "meta": {
            "sessions": len(sessions),
            "total_turns": total_turns,
            "findings": len(all_findings),
            "by_severity": dict(by_sev),
            "by_code": dict(by_code.most_common()),
            "high_by_module": dict(by_mod),
            "partial": partial,
        },
        "sessions": [
            {
                "module": s.module_title,
                "session_id": s.session_id,
                "errors": s.errors,
                "opening_preview": (s.opening or "")[:120],
                "turns": [asdict(t) for t in s.turns],
            }
            for s in sessions
        ],
        "high_findings": [asdict(f) for f in high[:80]],
        "all_findings": [asdict(f) for f in all_findings],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "kp-quality-report.json"
    md_path = OUT_DIR / "kp-quality-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    status = "（进行中 partial）" if partial else ""
    lines = [
        f"# AI 守秘人多会话质量报告{status}",
        "",
        f"- 会话数：{len(sessions)}",
        f"- 总轮次（含开场）：{total_turns}",
        f"- 问题标记：{len(all_findings)}（high={by_sev.get('high', 0)} med={by_sev.get('med', 0)} low={by_sev.get('low', 0)}）",
        "",
        "## 按问题类型",
        "",
    ]
    for code, n in by_code.most_common():
        lines.append(f"- `{code}` × {n}")
    lines += ["", "## 高严重度样本（最多 25）", ""]
    for f in high[:25]:
        lines.append(
            f"### [{f.severity}] {f.code} · {f.module} · t{f.turn}\n"
            f"- 玩家：{f.player}\n"
            f"- KP：{f.kp[:180]}{'…' if len(f.kp) > 180 else ''}\n"
            f"- 说明：{f.note}\n"
        )
    lines += ["", "## 按模组 high 计数", ""]
    for m, n in by_mod.most_common():
        lines.append(f"- {m}: {n}")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    if not partial:
        print("\n======== SUMMARY ========")
        print(json.dumps(report["meta"], ensure_ascii=False, indent=2))
        print("wrote", json_path)
        print("wrote", md_path)
    return by_sev


if __name__ == "__main__":
    raise SystemExit(main())
