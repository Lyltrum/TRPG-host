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
import random
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 真类型只给检查器看：运行时导入 `app.*` 会绕开下面那段 sys.path
    from app.core.keeper.runtime.agent import KeeperAgent
    from app.core.narration.contract import NarrationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

#: 默认轮次：覆盖「开场（该建场不该掷）」「元问题（该给引导）」「明确搜查（该触发检定）」
#: 🔴 **回放测试直接 import 这个常量**，不再各存一份——两边漂了的症状是
#: 请求指纹对不上，而那时人会以为是 prompt 变了，去重录一盘同样对不上的磁带。
#:
#: 🔴 **第三轮是 2026-08-16 重录时补的**：前两轮再也掷不出检定了，而这不是回归——
#: 第一轮被开场纪律吃掉（开场那一拍只建场、不发检定），第二轮是元问题该给引导。
#: 原来那两轮能录到结算纯属第一轮抢跑，抢跑本身正是我们后来治掉的毛病。
#: 补一轮**明确的搜查动作**，让「结算叙事」这条路径重新有一个不靠运气的来源。
DEFAULT_ROUNDS = [
    "我仔细查看四周，有什么不对劲的地方吗？",
    "我现在该做什么？",
    "我蹲下来，仔细查看地毯上那串泥脚印，看看它到底通向哪里。",
]

#: 掷骰种子。**录制与回放必须用同一个**：结算叙事的 prompt 里带着掷出的点数，
#: 骰子不一样，请求指纹就对不上。骰子本身不走模型，磁带录不到它。
TAPE_RNG_SEED = 20260811


async def play_round(keeper: KeeperAgent, context: NarrationContext) -> list[str]:
    """跑一轮，**并把这一轮发起的待掷检定全部结算掉**，返回每次拿到的正文。

    🔴 为什么要结算：不结算的话，只要裁决器在第一轮发起了检定，第二轮就会撞上
    `narrate()` 的待掷守卫——直接返回一句代码固定文案、**不调模型**。磁带于是
    少录一半，而"这一轮会不会发检定"是模型当下的输出，**覆盖形状因此靠运气**
    （同一份 prompt 连录三次是 2 条、改之前是 4 条）。

    结算之后还顺带覆盖了**结算叙事**那一拍——真实对局里最常见的一种回合，
    此前磁带一次都没录到过。

    类型标注走 `TYPE_CHECKING`：本模块不能在导入期拉起 `app.*`（脚本入口在
    `_run` 里才 import），但签名该说清楚它要什么。
    """
    # `NarrationContext.room_id` 在契约上可为 None（心跳那条路没有房间），
    # 而这里必然在一个真实房间里跑——显式失败，不给它静默滑过去的机会。
    assert context.room_id is not None, "play_round 要在一个真实房间里跑"
    texts: list[str] = []
    outcome = await keeper.narrate(context)
    texts.extend(_outcome_texts(outcome))
    # 逐个结算：队列清空的那一次，`resolve_check` 内部会复用 `narrate()`
    # 触发结算叙事，所以这里拿到的可能是一段真正的叙事。
    for request in list(outcome.check_requests):
        settled = await keeper.resolve_check(
            context.room_id,
            request.player_id,
            request.check_request_id,
        )
        texts.extend(_outcome_texts(settled))
    return texts


def _outcome_texts(outcome) -> list[str]:  # noqa: ANN001 — 见上：本模块不在导入期拉 app.*
    """这一次调用玩家实际看到的正文。

    🔴 待掷守卫那句固定文案走的是**按人裁的 segments**（`exec/23 #76`），不是
    全房间的 `text`。只读 `text` 的话，`test_the_tape_covers_a_settlement_turn`
    里那条「守卫文案不该出现」就变成结构上永远成立——一条自证的假绿。
    """
    if outcome.text:
        return [outcome.text]
    return [segment.text for segment in outcome.segments if segment.text]


async def _run(module_path: Path, out_path: Path, rounds: list[str]) -> int:
    # 🔴 都在函数里导入：本模块被 `tests/test_keeper_replay.py` import（两边
    # 共用轮次与跑法），而 `module_probe.probe` 要求它自己的目录先进 sys.path，
    # 放在模块顶层会让那条测试在收集阶段就炸。
    from app.core.coc7.content import build_coc7_ruleset
    from app.core.db import Base
    from app.core.keeper.contract.module_loader import load_module
    from app.core.keeper.runtime.agent import KeeperAgent
    from app.core.llm_tape import recording
    from app.core.narration.contract import NarrationContext
    from app.models.room import Character, Player, Room

    # 🔴 `module_probe.probe` 顶层 `from parallel import ...`——要求它自己的目录
    # 先进 sys.path，否则整条 import 在这里就炸（2026-08-16 重录时撞到：文档里
    # 写的那条命令根本跑不起来）。
    sys.path.insert(0, str(Path(__file__).resolve().parent / "module_probe"))
    from scripts.module_probe.probe import load_api_key

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
        rng=random.Random(TAPE_RNG_SEED),
    )

    # 磁带里的 scenario 就是模组路径——版权守卫据此判断能不能进 git。
    scenario = str(module_path).replace("\\", "/")
    with recording(out_path, scenario=scenario) as session:
        for i, utterance in enumerate(rounds, start=1):
            print(f"\n===== 轮次 {i} =====\n玩家：{utterance}", flush=True)
            for text in await play_round(
                keeper,
                NarrationContext(
                    utterance=utterance,
                    player_nickname=nickname,
                    room_id=room_id,
                    player_id=player_id,
                ),
            ):
                print(f"守秘人：{text}", flush=True)

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
