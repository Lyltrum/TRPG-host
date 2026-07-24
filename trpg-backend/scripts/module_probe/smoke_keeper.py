"""keeper 冒烟：用组装产物验证「老 schema 装下科比特后 keeper 能不能主持」。

目的不是粉饰跑通，而是当**缺口发现仪器**——冒烟中每一处别扭如实打印，
由人记进 模组资料/*.B验证观察.md（gitignored）。

硬约束：
- 不改 app/core/keeper/ 任何代码
- 临时库 + 干净房间，不碰生产 DB
- 不把模组原文写进任何 git 文件；stdout 里的叙事是 AI 生成，可进汇报

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/smoke_keeper.py \\
        --module ../模组资料/科比特先生.structured.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# 保证 app 与同目录 probe 可 import
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from probe import load_api_key  # noqa: E402


async def _run(module_path: Path, rounds: list[str]) -> int:
    from app.core.coc7_content import build_coc7_ruleset
    from app.core.db import Base
    from app.core.keeper.agent import KeeperAgent
    from app.core.keeper.module_loader import ModuleNode, load_module
    from app.core.narrator import NarrationContext
    from app.models.room import Character, Player, Room

    print(f"module: {module_path}", flush=True)
    module = load_module(module_path)
    print("load_module: ok", flush=True)
    print(f"  meta.id={module.meta.id!r} title={module.meta.title!r}", flush=True)
    print(f"  nodes={len(module.nodes)}", flush=True)
    print(f"  npcs={len(module.npcs)}", flush=True)
    print(f"  endings={len(module.endings)}", flush=True)
    print(f"  agenda={len(module.agenda)}", flush=True)

    def _walk_nodes(nodes: list[ModuleNode]) -> list[ModuleNode]:
        out: list[ModuleNode] = []
        for n in nodes:
            out.append(n)
            if n.sub_node is not None:
                out.extend(_walk_nodes([n.sub_node]))
            if n.sub_nodes:
                out.extend(_walk_nodes(n.sub_nodes))
        return out

    all_nodes = _walk_nodes(module.nodes)
    check_n = sum(len(n.checks) for n in all_nodes)
    sub_nodes_n = sum(len(n.sub_nodes) for n in all_nodes)
    forms_n = sum(len(n.forms) for n in module.npcs)
    print(f"  checks={check_n}", flush=True)
    print(f"  sub_nodes_total={sub_nodes_n}", flush=True)
    print(f"  npc forms_total={forms_n}", flush=True)
    print(f"  visibility_pairs={len(module.visibility_pairs)}", flush=True)
    print(f"  kp_guidance keys={list(module.kp_guidance.keys())}", flush=True)
    print("  node ids:", [n.id for n in module.nodes], flush=True)
    print("  npc ids:", [n.id for n in module.npcs], flush=True)
    print("  ending ids:", [e.id for e in module.endings], flush=True)
    print("  agenda ids:", [a.id for a in module.agenda], flush=True)

    # 拓扑观察（缺口仪器）：三类边
    print("\n--- exits 图（空间邻接）---", flush=True)
    for n in all_nodes:
        if n.exits:
            print(f"  {n.id} ↔ {n.exits}", flush=True)
    print("\n--- contains 边（包含）---", flush=True)
    for n in all_nodes:
        if n.contains:
            print(f"  {n.id} ⊃ {n.contains}", flush=True)
    print("\n--- leads_to 图（情节推进）---", flush=True)
    for n in all_nodes:
        print(f"  {n.id} → {n.leads_to}", flush=True)

    print("\n--- npc stats / forms / same_as ---", flush=True)
    for npc in module.npcs:
        keys = list((npc.stats or {}).keys()) if npc.stats else []
        notes_len = len(npc.kp_notes or "")
        form_ids = [f.id for f in npc.forms]
        print(
            f"  {npc.id} name={npc.name!r} role={npc.role!r} "
            f"stats_keys={keys} forms={form_ids} same_as={npc.same_as} "
            f"kp_notes_len={notes_len}",
            flush=True,
        )
    if module.visibility_pairs:
        print("\n--- visibility_pairs ---", flush=True)
        for p in module.visibility_pairs:
            print(
                f"  {p.id}: {p.public_ref} ↔ {p.secret_ref} ({p.note or ''})",
                flush=True,
            )

    api_key = load_api_key()
    ruleset = build_coc7_ruleset()

    db_path = Path(tempfile.mkdtemp(prefix="trpg-smoke-")) / "smoke.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        room = Room(
            room_code="SMOKE1",
            room_name="组装冒烟房",
            max_players=4,
            phase="InGame",
            keeper_state={},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="调查员甲", is_host=True)
        db.add(player)
        await db.flush()
        char = Character(
            room_id=room.id,
            player_id=player.id,
            status="ready",
            name="调查员甲",
            occupation="私家侦探",
            generation_method="pointbuy",
            attributes={
                "STR": 50,
                "CON": 60,
                "POW": 55,
                "DEX": 65,
                "APP": 50,
                "SIZ": 55,
                "INT": 70,
                "EDU": 75,
                "LUCK": 60,
            },
            derived_stats={"HP": 11, "HP_MAX": 11, "MP": 11, "SAN": 55, "SAN_MAX": 55},
            skills={
                "spot-hidden": 60,
                "listen": 50,
                "library-use": 50,
                "psychology": 45,
                "fast-talk": 40,
                "stealth": 40,
                "fighting-brawl": 40,
            },
            background="",
            notes="",
        )
        db.add(char)
        player.has_character = True
        await db.commit()
        room_id = room.id
        player_id = player.id
        nickname = player.nickname

    print(f"\nroom_id={room_id} player_id={player_id} db={db_path}", flush=True)

    keeper = KeeperAgent(
        api_key=api_key,
        module=module,
        ruleset=ruleset,
        session_factory=session_factory,
    )

    smoke_log: list[dict] = []

    for i, utterance in enumerate(rounds, start=1):
        print(f"\n========== 轮次 {i} ==========", flush=True)
        print(f"玩家：{utterance}", flush=True)
        ctx = NarrationContext(
            utterance=utterance,
            player_nickname=nickname,
            room_id=room_id,
            player_id=player_id,
        )
        outcome = await keeper.narrate(ctx)
        entry: dict = {
            "round": i,
            "utterance": utterance,
            "narration": outcome.text,
            "check_requests": [
                {
                    "check_request_id": c.check_request_id,
                    "kind": c.kind,
                    "skill": c.skill,
                    "reason": c.reason,
                    "player_nickname": c.player_nickname,
                }
                for c in (outcome.check_requests or [])
            ],
        }
        print("--- 叙事 ---", flush=True)
        print(outcome.text or "(空)", flush=True)
        if outcome.check_requests:
            print("--- 待掷检定 ---", flush=True)
            for c in outcome.check_requests:
                print(
                    f"  id={c.check_request_id} kind={c.kind} "
                    f"skill={c.skill!r} reason={c.reason!r}",
                    flush=True,
                )
            # 自动结算第一张待掷，走通 裁决→执行→掷骰→叙事
            first = outcome.check_requests[0]
            print(f"\n--- 自动掷骰 {first.check_request_id} ---", flush=True)
            resolved = await keeper.resolve_check(room_id, player_id, first.check_request_id)
            entry["check_results"] = [
                {
                    "check_request_id": r.check_request_id,
                    "kind": r.kind,
                    "skill": r.skill,
                    "rolled": r.rolled,
                    "target": r.target,
                    "level": r.level,
                    "san_loss": r.san_loss,
                    "san_remaining": r.san_remaining,
                }
                for r in (resolved.check_results or [])
            ]
            entry["post_roll_narration"] = resolved.text
            print("--- 掷骰结果 ---", flush=True)
            for r in resolved.check_results or []:
                print(
                    f"  {r.kind} skill={r.skill!r} rolled={r.rolled}/{r.target} → {r.level}",
                    flush=True,
                )
            print("--- 掷骰后叙事 ---", flush=True)
            print(resolved.text or "(空)", flush=True)
            # 若还有链式待掷，也清掉以免卡住
            while resolved.check_requests:
                nxt = resolved.check_requests[0]
                print(f"\n--- 链式掷骰 {nxt.check_request_id} ---", flush=True)
                resolved = await keeper.resolve_check(room_id, player_id, nxt.check_request_id)
                print(resolved.text or "(空)", flush=True)

        smoke_log.append(entry)

    # 读最终 keeper_state
    async with session_factory() as db:
        room2 = await db.get(Room, room_id)
        state = dict(room2.keeper_state or {}) if room2 else {}
    print("\n========== 最终 keeper_state ==========", flush=True)
    print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)

    # 把冒烟日志写到模组同目录（gitignored）
    out = module_path.with_name(module_path.name.replace(".structured.json", ".冒烟日志.json"))
    if out == module_path:
        out = module_path.with_suffix(module_path.suffix + ".冒烟日志.json")
    out.write_text(
        json.dumps(
            {
                "module": str(module_path),
                "counts": {
                    "nodes": len(module.nodes),
                    "npcs": len(module.npcs),
                    "endings": len(module.endings),
                    "agenda": len(module.agenda),
                    "checks": check_n,
                },
                "leads_to": {n.id: n.leads_to for n in module.nodes},
                "npc_stats_keys": {
                    n.id: list((n.stats or {}).keys()) if n.stats else [] for n in module.npcs
                },
                "rounds": smoke_log,
                "final_keeper_state": state,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote smoke log: {out}", flush=True)

    await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="keeper 冒烟（组装产物）")
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument(
        "--round",
        action="append",
        dest="rounds",
        default=None,
        help="玩家行动文本，可多次；默认三轮（观察+检定向+对话）",
    )
    args = parser.parse_args(argv)
    rounds = args.rounds or [
        # 纯对话/观察，开场
        "我站在自己家窗边，观察对面邻居的房子，看看有没有什么异常。",
        # 调查线索：报纸/图书馆（验证线索是否作为 node 内容被用上，而非蒸发）
        "我去镇上的报社和图书馆，查阅当地报纸过刊，看看有没有关于科比特家或相关人物的旧闻。",
        # 应触发检定（侦察/聆听一类）
        "我悄悄走到对面房子的花园边，仔细搜索灌木和花丛，看有没有可疑痕迹。",
        # 对话/社交向
        "如果科比特先生在家，我去按门铃，礼貌地和他聊两句最近的事。",
    ]
    return asyncio.run(_run(args.module, rounds))


if __name__ == "__main__":
    raise SystemExit(main())
