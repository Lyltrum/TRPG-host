"""导入 job：状态机 + 启动清扫 + 剧透约束的 schema 执行（`exec/29 §7.2`）。

三组不变量，每一组坏掉的症状都不一样：

1. **清扫**坏了 → 用户的 job 永远显示"进行中"，而没有任何进程在跑它。
2. **`interrupted` 跟 `failed` 混了** → 用户以为自己的模组转不了（其实是我们
   重启了）。
3. **剧透约束**坏了 → 模组内容跨到前端，这个功能的全部意义就没了。

第 3 组是这个文件里最重要的：它不检查"我们记得没写正文"，而检查**表结构里
根本没有能装下正文的地方**。同「保密靠拿不到，不是请你别说」。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.module_import.job_state import (
    ALL_STATUSES,
    FAILURE_KINDS,
    STAGES,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    is_terminal,
    normalize_failure_kinds,
    stage_index,
)
from app.core.module_import.sweep import sweep_stale_jobs
from app.models.replay import ModuleImportJob

_db_path = Path(tempfile.mkdtemp(prefix="trpg-import-job-test-")) / "job.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


# ── 1. 启动清扫 ───────────────────────────────────────


async def test_running_and_pending_are_both_swept() -> None:
    """🔴 `pending` 也要扫。

    它是"已收下文件、还没开始跑"，同样只活在进程内存的调度里——进程没了它
    永远不会被捡起来，**留在 pending 就是永久转圈**，比 running 还隐蔽。
    """
    async with _session_factory() as db:
        db.add_all(
            [
                ModuleImportJob(status=STATUS_RUNNING, stage="assembling"),
                ModuleImportJob(status=STATUS_PENDING, stage="received"),
            ]
        )
        await db.commit()

        assert await sweep_stale_jobs(db) == 2
        rows = (await db.scalars(select(ModuleImportJob))).all()
        assert {r.status for r in rows} == {STATUS_INTERRUPTED}
        assert all(r.finished_at is not None for r in rows)


async def test_terminal_jobs_are_left_alone() -> None:
    """已经有结论的不许被改写——失败理由是用户唯一能看的东西。"""
    async with _session_factory() as db:
        db.add_all(
            [
                ModuleImportJob(status=STATUS_SUCCEEDED, stage="registering"),
                ModuleImportJob(status=STATUS_FAILED, error_message="这份文稿抽不出任何条目"),
            ]
        )
        await db.commit()

        assert await sweep_stale_jobs(db) == 0
        failed = (
            await db.scalars(select(ModuleImportJob).where(ModuleImportJob.status == STATUS_FAILED))
        ).one()
        assert failed.error_message == "这份文稿抽不出任何条目"


async def test_sweep_on_empty_table_is_a_noop() -> None:
    async with _session_factory() as db:
        assert await sweep_stale_jobs(db) == 0


async def test_interrupted_is_not_failed() -> None:
    """🔴 作废不是失败：这不是模组的问题，是我们的进程没了。

    理由文案必须指向"重新导入"，而不是"这份模组不行"。
    """
    async with _session_factory() as db:
        db.add(ModuleImportJob(status=STATUS_RUNNING))
        await db.commit()
        await sweep_stale_jobs(db)

        job = (await db.scalars(select(ModuleImportJob))).one()

    assert job.status == STATUS_INTERRUPTED != STATUS_FAILED
    assert job.error_message and "重新导入" in job.error_message


async def test_sweep_never_reruns_anything() -> None:
    """🔴 绝不自动重跑——重启后默默再花一次钱（¥0.35 / 71 次调用）是最坏的。

    表现为：清扫只写终态，不产生任何新 job。
    """
    async with _session_factory() as db:
        db.add(ModuleImportJob(status=STATUS_RUNNING, source_filename="x.pdf"))
        await db.commit()
        await sweep_stale_jobs(db)

        assert len((await db.scalars(select(ModuleImportJob))).all()) == 1


# ── 2. 状态机本身 ─────────────────────────────────────


def test_terminal_statuses_are_exactly_the_three() -> None:
    assert is_terminal(STATUS_SUCCEEDED)
    assert is_terminal(STATUS_FAILED)
    assert is_terminal(STATUS_INTERRUPTED)
    assert not is_terminal(STATUS_RUNNING)
    assert not is_terminal(STATUS_PENDING)


def test_unknown_stage_is_not_reported_as_the_beginning() -> None:
    """未知阶段返回 -1，**不是 0** —— 0 会让它看起来刚开始，进度条直接撒谎。"""
    assert stage_index("received") == 0
    assert stage_index("registering") == len(STAGES) - 1
    assert stage_index("没这个阶段") == -1


def test_stage_order_matches_the_pipeline() -> None:
    """阶段是给用户看的，不是内部调用图——五步管线 + 落库，多一步都不该有。"""
    assert STAGES == (
        "received",
        "extracting",
        "probing",
        "relating",
        "assembling",
        "registering",
    )


# ── 3. 🔴 剧透约束靠 schema 执行 ──────────────────────


def test_failure_kinds_keep_only_the_category_word() -> None:
    """🔴 错误原文里带着实体 id、数值、半句原文，跨到前端的只能是类别词。"""
    kinds = normalize_failure_kinds(
        [
            "[numeric] node 'mi-go' 的数值 '701d6+1' 在原文里找不到——疑似凭空生成",
            "[skill] node 'outside-house' checks[1] 未归一到技能 id（原文 '动物学'）",
            "[numeric] 又一条",
        ]
    )

    assert kinds == ["numeric", "skill"]


def test_unknown_prefix_is_dropped_not_passed_through() -> None:
    """🔴 不做"未知类别"兜底——那个兜底会把任意字符串放行到前端。"""
    assert normalize_failure_kinds(["[某个真相] 泄露了", "没有方括号的一行"]) == []


def test_every_failure_kind_is_a_bare_word() -> None:
    """封闭集合里的词本身不能携带信息——都是短的英文标识符。"""
    assert all(k.isascii() and k.islower() and len(k) <= 14 for k in FAILURE_KINDS)


def test_job_table_has_no_free_form_content_column() -> None:
    """🔴 本文件最重要的一条：**表里没有能装下模组正文的地方**。

    这条不检查"我们记得没写正文"，检查的是结构。允许的自由文本只有两处，
    每一处都有明确理由：

    - `source_filename` —— 用户自己起的文件名，不是模组内容
    - `error_message`   —— 拒绝理由，由代码生成（只说数量与类别）
    - `source_path`     —— 服务器上的上传件路径，**内部字段，不进 DTO**

    再加一个就要先回答：**它能不能装下一句剧透？**
    """
    columns = {c.key: c for c in inspect(ModuleImportJob).columns}
    textual = {
        name
        for name, col in columns.items()
        if col.type.__class__.__name__ in ("Text", "String", "JSON")
    }

    # id / owner_user_id / result_scenario_id / retried_from_job_id 是 Uuid 列，
    # 装不下自由文本，不在这个集合里。
    assert textual == {
        "status",
        "stage",
        "source_filename",
        "source_sha256",
        "source_path",
        "error_message",
        "failure_kinds",
    }


def test_report_numbers_are_separate_integer_columns() -> None:
    """报告是数量与拓扑，所以它是一组 Integer，不是一个 `stats: JSON`。"""
    columns = {c.key: c for c in inspect(ModuleImportJob).columns}
    counters = [
        "page_count",
        "image_count",
        "char_count",
        "item_count",
        "node_count",
        "npc_count",
        "ending_count",
        "agenda_count",
        "hard_failure_count",
    ]

    assert all(columns[c].type.__class__.__name__ == "Integer" for c in counters)
    # 🔴 没有 `llm_call_count`：整条链的调用散在三个脚本里，只有 assemble 会报数。
    # 一个只覆盖三分之一却叫"调用次数"的列，是半真值，不如不立。
    assert "llm_call_count" not in columns


def test_every_report_column_has_a_writer() -> None:
    """🔴 **守的是"有没有人写"，不是"列在不在"**（2026-08-19 补）。

    上面那条只断言列存在且是 Integer——那是 schema。而「加了字段没有消费方」
    的镜面版本是**有消费方但没有数据**，项目 CLAUDE.md 明写"两个方向都不会
    变红"：列有、DTO 有、前端读得到，只要没人赋值，它就永远是 0，而两条既有
    测试都不会察觉。

    ## 为什么扫 AST 而不是逐个调函数

    写入方**分在两处**：`_copy_counts` 抄管线量到的五个（页数/图片/字符/条目/
    硬失败），`_register` 直接从 module 数四个拓扑数（节点/NPC/结局/议程）。
    逐个列出调用点就是「逐个列出的地方，加一项就漏一项」——所以这里**扫赋值
    语句的左侧**，新加一个 `*_count` 列却没人写，它当场红。

    （🔴 写这条测试时我先断言过"那四个数恒为 0"，**是错的**——只读了
    `_copy_counts` 就下结论，而 `_register` 里那四行就在它上面一行。库里成功
    那条 job 是 `node=12 npc=6`。教训进 `wrong-measurement-target`。）
    """
    import ast
    from pathlib import Path as _Path

    columns = {c.key for c in inspect(ModuleImportJob).columns if c.key.endswith("_count")}
    source = (
        _Path(__file__).parent.parent / "app" / "core" / "module_import" / "runner.py"
    ).read_text(encoding="utf-8")

    written: set[str] = set()

    def _collect(target: ast.expr) -> None:
        # 🔴 **要认元组解包**（2026-08-25 补）：写成
        # `job.a_count, job.b_count = f(...)` 时 target 是 `ast.Tuple`，
        # 只认 `ast.Attribute` 的话这两列会被报成"没有写入方"——**而它们明明有**。
        # 这条守护自己的盲区，是被一次真实的假红当场撞出来的。
        if isinstance(target, ast.Tuple):
            for element in target.elts:
                _collect(element)
        elif isinstance(target, ast.Attribute) and target.attr.endswith("_count"):
            written.add(target.attr)

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            _collect(target)

    missing = columns - written
    assert not missing, f"这些报告列没有任何写入方，会永远停在 0：{sorted(missing)}"


def test_ledger_counts_walk_into_sub_nodes() -> None:
    """🔴 线索账本那两个数要**遍历全部节点**（`exec/46` B1，2026-08-25）。

    样本刻意造成**顶层与全部的答案不同**：带 `reveals` 的那条 check 挂在
    **子节点**上，顶层那个节点自己一条都没有 ⇒ 只数顶层得 0、数全部得 1。
    变异体「`iter_all_nodes(module.nodes)` 换成 `module.nodes`」当场红。

    （造样本时的老坑：样本里每个字段都不许等于默认值，否则漏算恰好"猜对"。）
    """
    from app.core.keeper.contract.module_loader import ScenarioModule
    from app.core.module_import.runner import ledger_counts

    module = ScenarioModule.model_validate(
        {
            "meta": {"id": "m", "title": "t"},
            "kp_truth": {"summary": "真相"},
            "player_intro": "开场",
            "facts": [
                {"id": "f-1", "text": "一"},
                {"id": "f-2", "text": "二"},
                {"id": "f-3", "text": "三"},
            ],
            "nodes": [
                {
                    "id": "top",
                    "title": "顶层",
                    "kp_text": "顶层正文",
                    # 顶层这条**故意不标 reveals**：只数顶层就会得 0
                    "checks": [{"skill_ids": ["spot-hidden"], "difficulty": "normal"}],
                    "sub_nodes": [
                        {
                            "id": "sub",
                            "title": "子节点",
                            "kp_text": "子节点正文",
                            "checks": [
                                {
                                    "skill_ids": ["library-use"],
                                    "difficulty": "normal",
                                    "reveals": ["f-1"],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    facts, revealing = ledger_counts(module)
    assert facts == 3
    assert revealing == 1, "带 reveals 的检定点挂在子节点上，只数顶层会漏掉它"


def test_the_ledger_columns_can_tell_never_measured_from_really_zero() -> None:
    """🔴 `None`（这次没量过）与 `0`（量过、确实是零）**含义相反，不许压成一个值**。

    契约层面：两列可空、DTO 是 `int | None` 且**必填**——"必填但可为 null"
    跟"可以不发"是两回事（2026-08-19 那条判据）。给个 `default=0` 的话，
    改动之前那些从没量过的旧 job 会在界面上显示成"这份模组没有线索"，
    那是**编出来的结论**。
    """
    from app.dto.module import ModuleImportJobRead

    columns = {c.key: c for c in inspect(ModuleImportJob).columns}
    for col in ("fact_count", "revealing_check_count"):
        assert columns[col].nullable, f"{col} 必须可空，否则 None 与 0 分不开"
        field = ModuleImportJobRead.model_fields[col]
        assert field.is_required(), f"{col} 在 DTO 里必须是必填（可为 null，但一定要发）"


def test_the_five_pipeline_counts_are_copied_verbatim() -> None:
    """`_copy_counts` 抄的那五个，一个都不许漏。

    样本取**互不相同的非零值**：漏抄一个它会停在 0，与样本不等 ⇒ 红。
    有 0 的话，漏抄它就恰好"猜对"了（同 `exec/40` 守护测试瞎掉的那条修法：
    **造的样本不许有任何字段等于默认值**）。

    **变异检验**：删掉 `_copy_counts` 里任意一行，这条当场红。
    """
    from types import SimpleNamespace

    from app.core.module_import.runner import _copy_counts

    sample = SimpleNamespace(page_count=11, image_count=22, chars=33, items=44, hard_failures=99)
    expected = {
        "page_count": 11,
        "image_count": 22,
        "char_count": 33,
        "item_count": 44,
        "hard_failure_count": 99,
    }
    assert 0 not in expected.values(), "样本里有 0 ⇒ 漏抄那一列会蒙混过关"

    job = ModuleImportJob()
    for column in expected:
        setattr(job, column, 0)
    _copy_counts(job, sample)

    assert {c: getattr(job, c) for c in expected} == expected


def test_the_report_columns_and_the_dto_do_not_drift() -> None:
    """报告列与 DTO 字段一一对应——加了列不暴露，或暴露了没有列，都算漂。

    🔴 **逐个列出的地方，加一项就漏一项**：这里改成两边**扫集合**再比对，
    而不是各自维护一张手写清单。
    """
    from app.dto.module import ModuleImportJobRead

    columns = {c.key for c in inspect(ModuleImportJob).columns if c.key.endswith("_count")}
    fields = {name for name in ModuleImportJobRead.model_fields if name.endswith("_count")}
    assert columns == fields, (
        f"列与 DTO 不一致：只在列里 {columns - fields}，只在 DTO 里 {fields - columns}"
    )


def test_status_vocabulary_is_closed() -> None:
    assert {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_SUCCEEDED,
        STATUS_FAILED,
        STATUS_INTERRUPTED,
    } == ALL_STATUSES


# ── 降级交付（2026-08-20）──────────────────────────────


def test_only_the_listed_kinds_are_degradable() -> None:
    """🔴 **分档表是白名单，不是黑名单。**

    新加一类校验错误时，它默认**不可降级**——那是安全的那一侧。反过来写
    （"除了这几类都能降级"）会让任何新错误静默放行。
    """
    from app.core.module_import.job_state import DEGRADABLE_FAILURE_KINDS, is_degradable

    assert set(FAILURE_KINDS) >= DEGRADABLE_FAILURE_KINDS, "分档表里有不存在的类别"
    for kind in FAILURE_KINDS:
        if kind not in DEGRADABLE_FAILURE_KINDS:
            assert not is_degradable([kind]), f"{kind} 不在白名单里却能降级"


def test_the_two_hard_gates_stay_hard() -> None:
    """🔴 用户明确同意保留硬门的两类，**不许被降级放行**。

    - leak —— 剧透第一性，是这条线三层判据的第一层；
    - content_preserve —— 改到连原文都对不上，那不是修好了，是编了一份。

    **变异检验**：把 "leak" 加进 DEGRADABLE_FAILURE_KINDS，这条当场红。
    """
    from app.core.module_import.job_state import is_degradable

    assert not is_degradable(["leak"])
    assert not is_degradable(["trace", "leak"]), "混着一条硬门也不许整批降级"


def test_a_mixed_batch_needs_every_kind_to_be_degradable() -> None:
    """一批里只要有一类不可降级，整批就得拒——不是"多数可降级就降级"。"""
    from app.core.module_import.job_state import is_degradable

    assert is_degradable(["trace", "reach"])
    assert not is_degradable(["trace", "schema"])


def test_no_kinds_at_all_is_not_degradable() -> None:
    """🔴 空集合返回 False。

    一次没有任何类别的失败，说明**分类器本身没认出来**——那时"能降级"是个
    没有依据的结论。缺数据要显式失败，不静默放行。

    **变异检验**：去掉那个空集合判断（`all([])` 是 True），这条当场红。
    """
    from app.core.module_import.job_state import is_degradable

    assert not is_degradable([])


def test_the_runner_degrades_before_it_gives_up() -> None:
    """🔴 **守接线**：runner 必须在 `_finish_failed` 之前先问一句能不能降级。

    只测 `is_degradable` 是不够的——那是纯函数，它返回 True 不代表 runner 会
    去调它（「加了函数没有消费方」）。

    **变异检验**：把 runner 里那个 `if is_degradable(kinds):` 分支删掉，
    这条当场红。
    """
    import ast
    from pathlib import Path as _Path

    source = (
        _Path(__file__).parent.parent / "app" / "core" / "module_import" / "runner.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "is_degradable"
    ]
    assert calls, "runner 从来没调用过 is_degradable —— 降级这条路接不上"

    # 而且降级分支里要真的去注册，不是只记个日志
    assert "caveats=kinds" in source, "降级时没把 caveats 传给 _register"


# ── 5. 标题线索（A9）─────────────────────────────────


async def test_the_runner_hands_the_real_filename_down_as_a_title_hint(
    tmp_path, monkeypatch
) -> None:
    """🔴 **守接线**：组装层拿到的标题提示必须是真实文件名，不是那串 sha256。

    上传件按内容哈希存盘（`uploads/f43acfd8….pdf`），而组装层原来是从
    `extract_path` 的词干推标题提示 ⇒ web 导入这条路上「模组标题提示」一直是
    一串十六进制，等于**没有提示**。74 节点那份因此把正文里的一个小标题当成了
    模组名（`meta.id` 反而对——抽 id 那一步看的不是它）。

    **这条只能走接线验**：`assemble.run_pipeline` 收不收 `title_hint` 是纯函数，
    它有这个参数不代表 runner 会去传（「加了函数没有消费方」）。原来的手工跑法
    （`林中屋.重组.裸抽取.json`）一直是对的，正是「换一条路径验出来的『好了』」。

    **变异检验**：把 runner 里的 `title_hint=title_hint` 删掉，这条当场红。
    """
    from app.core.module_import import runner as runner_module

    seen: dict[str, object] = {}

    class _FakeConversionError(Exception):
        pass

    class _FakePipeline:
        ConversionError = _FakeConversionError

        @staticmethod
        def convert(source, **kwargs):
            seen.update(kwargs)
            seen["source"] = source
            raise _FakeConversionError("到此为止——只验参数")

    monkeypatch.setattr(runner_module, "_load_pipeline", lambda: _FakePipeline)

    upload = tmp_path / ("f" * 64 + ".pdf")
    upload.write_bytes(b"%PDF-")
    async with _session_factory() as db:
        job = ModuleImportJob(
            status=STATUS_PENDING,
            stage="received",
            source_filename="[某翻译组]一份长模组 序章：装置.pdf",
            source_path=str(upload),
        )
        db.add(job)
        await db.commit()
        job_id = job.id

    await runner_module.run_import_job(
        job_id, session_factory=_session_factory, work_root=tmp_path / "work"
    )

    assert seen, "装置自证：convert 根本没被调到，下面的断言就没有意义"
    assert Path(str(seen["source"])).name.startswith("f" * 8), "装置自证：上传件确实是哈希命名的"
    hint = seen.get("title_hint")
    assert hint == "[某翻译组]一份长模组 序章：装置", (
        f"组装层拿到的标题提示是 {hint!r}——它必须来自真实文件名"
    )


async def test_a_job_without_a_filename_still_runs(tmp_path, monkeypatch) -> None:
    """没有文件名不是错误，退回管线自己的推法即可——**别静默塞一个假标题**。"""
    from app.core.module_import import runner as runner_module

    seen: dict[str, object] = {}

    class _FakeConversionError(Exception):
        pass

    class _FakePipeline:
        ConversionError = _FakeConversionError

        @staticmethod
        def convert(source, **kwargs):
            seen.update(kwargs)
            raise _FakeConversionError("到此为止")

    monkeypatch.setattr(runner_module, "_load_pipeline", lambda: _FakePipeline)
    upload = tmp_path / "anon.pdf"
    upload.write_bytes(b"%PDF-")
    async with _session_factory() as db:
        job = ModuleImportJob(status=STATUS_PENDING, stage="received", source_path=str(upload))
        db.add(job)
        await db.commit()
        job_id = job.id

    await runner_module.run_import_job(
        job_id, session_factory=_session_factory, work_root=tmp_path / "work"
    )
    assert seen, "装置自证：convert 没被调到"
    assert seen["title_hint"] is None


def test_a_hashy_stem_is_not_passed_off_as_a_title() -> None:
    """🔴 `resolve_title_hint` 的另一半：**没有线索就说没有，别假装有。**

    上面那条守的是 runner 会不会把真实文件名传下去；这条守的是组装层拿到
    `None` 时的行为——退回词干，而词干看着像哈希就**明说不知道**。塞一串
    十六进制进 prompt 是「静默兜底」：模型会当它是个标题去用。
    """
    # 走包路径而不是往 sys.path 插目录——同 `runner._load_pipeline`，
    # 那是这个仓库里唯一被验证过的导入姿势。
    from scripts.module_probe.assemble import resolve_title_hint  # noqa: PLC0415

    hashy = Path("f43acfd839bd68f997739fb4ad70996d90e88d691a8bcbabd4e3316d0.裸抽取.json")
    assert "f43acfd8" not in resolve_title_hint(None, hashy)
    assert "未知" in resolve_title_hint(None, hashy)
    # 传进来的真实文件名永远优先
    real = "一份长模组 序章：装置"
    assert resolve_title_hint(real, hashy) == real
    # 手工跑法那条路不许被改坏
    assert resolve_title_hint(None, Path("林中屋.重组.裸抽取.json")) == "林中屋"
    assert resolve_title_hint("   ", Path("林中屋.重组.裸抽取.json")) == "林中屋"


def test_run_pipeline_actually_calls_the_resolver() -> None:
    """🔴 **「加了函数没有消费方」的守门人。**

    `resolve_title_hint` 单测再全，也不代表 `run_pipeline` 会去调它——而这中间
    要真跑一遍 LLM 才验得到，所以退回读源码。同 `_the_runner_degrades_before…`。

    **变异检验**：把 `run_pipeline` 里那一行换回
    `extract_path.name.split(".")[0]`，这条当场红（其余 25 条一条都不会红）。
    """
    import ast
    from pathlib import Path as _Path

    source = (_Path(__file__).parent.parent / "scripts" / "module_probe" / "assemble.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    run_pipeline = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_pipeline"
    )
    called = {
        node.func.id
        for node in ast.walk(run_pipeline)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_title_hint" in called, (
        "run_pipeline 没有调 resolve_title_hint —— 标题线索这条路接不上"
    )
    # 而且它得把调用方传进来的那个参数喂进去，不是只拿 extract_path 自己算
    assert "resolve_title_hint(title_hint, extract_path)" in source
