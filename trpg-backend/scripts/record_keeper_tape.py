"""录一盘 keeper 磁带（exec/14 P0）。

跑一个临时库 + 干净房间，用真实 DeepSeek 走 N 轮，把每次 LLM 往返录进磁带。
之后 `tests/test_keeper_replay.py` 就能在断网状态下重放这批模型输出，断言
代码行为不变——这是 P1/P2 大重构的安全网。

🔴 版权：真实模组的磁带含剧本正文，只能落 gitignored 的 `tapes/`。
只有原创迷你剧本 `tests/fixtures/keeper_module.json` 的磁带允许进 `tests/tapes/`
（`test_llm_tape.py` 的守卫测试会兜底）。

用法（在 trpg-backend/ 下）：

    # 可提交的基线（原创迷你剧本）
    .venv/bin/python scripts/record_keeper_tape.py \\
        --module tests/fixtures/keeper_module.json \\
        --out tests/tapes/keeper_minimal.json

    # 真实模组（只能留本地）
    .venv/bin/python scripts/record_keeper_tape.py \\
        --module ../模组资料/追书人.structured.json \\
        --out tapes/zhuishuren.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from scripts.module_probe.probe import load_api_key  # noqa: E402

#: 默认轮次：覆盖「隐式搜查（该触发检定）」「直接对话」「元问题（该给引导）」
DEFAULT_ROUNDS = [
    "我仔细查看四周，有什么不对劲的地方吗？",
    "我现在该做什么？",
]


async def _run(module_path: Path, out_path: Path, rounds: list[str]) -> int:
    from app.core.coc7_content import build_coc7_ruleset
    from app.core.db import Base
    from app.core.keeper.contract.module_loader import load_module
    from app.core.keeper.runtime.agent import KeeperAgent
    from app.core.llm_tape import recording
    from app.core.narration.contract import NarrationContext
    from app.models.room import Character, Player, Room

    module = load_module(module_path)
    ruleset = build_coc7_ruleset()
    api_key = load_api_key()

    db_path = Path(tempfile.mkdtemp(prefix="trpg-tape-")) / "tape.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        room = Room(
            room_code="TAPE01",
            room_name="磁带录制房",
            max_players=4,
            phase="InGame",
            keeper_state={},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="调查员甲", is_host=True)
        db.add(player)
        await db.flush()
        db.add(
            Character(
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
        )
        player.has_character = True
        await db.commit()
        room_id, player_id, nickname = room.id, player.id, player.nickname

    keeper = KeeperAgent(
        api_key=api_key,
        module=module,
        ruleset=ruleset,
        session_factory=session_factory,
    )

    # 磁带里的 scenario 就是模组路径——版权守卫据此判断能不能进 git。
    scenario = str(module_path).replace("\\", "/")
    with recording(out_path, scenario=scenario) as session:
        for i, utterance in enumerate(rounds, start=1):
            print(f"\n===== 轮次 {i} =====\n玩家：{utterance}", flush=True)
            outcome = await keeper.narrate(
                NarrationContext(
                    utterance=utterance,
                    player_nickname=nickname,
                    room_id=room_id,
                    player_id=player_id,
                )
            )
            print(f"守秘人：{outcome.text}", flush=True)
            if outcome.check_requests:
                print(
                    "  检定请求：" + ", ".join(c.skill or c.kind for c in outcome.check_requests),
                    flush=True,
                )

    print(
        f"\n磁带已写入 {out_path}：{len(session.tape.entries)} 次调用 "
        f"({[e.kind for e in session.tape.entries]})",
        flush=True,
    )
    await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="录一盘 keeper 磁带")
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--round", action="append", dest="rounds", default=None)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.module, args.out, args.rounds or DEFAULT_ROUNDS))


if __name__ == "__main__":
    raise SystemExit(main())
