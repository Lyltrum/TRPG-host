"""把转换管线接进 job：跑、报进度、注册或拒绝（`exec/29` 第 5 步）。

## 🔴 管线是同步阻塞的，必须扔进线程

`pipeline.convert` 是一条同步的链，实测 **5–26 分钟**。直接在事件循环里 `await`
它等于**整个后端冻住 20 分钟**——所有房间的 WebSocket、所有 HTTP 请求一起停。
所以走 `asyncio.to_thread`，而阶段回调发生在工作线程里，得用
`run_coroutine_threadsafe` 把写库调度回主循环。

## 🔴 管线住在 `scripts/`，这是一笔明账

整条转换链（`probe` / `relation_probe` / `assemble` / `pipeline`）都在
`trpg-backend/scripts/module_probe/`。`app/` 反过来 import `scripts/` 是层次倒挂，
属于「`pipeline.py` 归 `app/` 还是 `scripts/`」这笔早就记着的欠账。

现在不搬：搬要同时搬四个脚本 + 它们的 CLI 入口，跟本步骤（把 job 跑起来）无关。
但欠账写在这里，别让它变成"没人知道为什么 app 在 import scripts"。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.module_import.job_state import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    normalize_failure_kinds,
)
from app.core.seed import BUILTIN_SYSTEM_ID
from app.models.content import ImportedModule, Scenario
from app.models.replay import ModuleImportJob

logger = structlog.get_logger()


def _load_pipeline() -> Any:
    """惰性导入管线：它会拉起 openai 客户端等一堆东西，不该在应用启动时就付。

    `pipeline` 模块自己会把 `scripts/module_probe/` 插进 `sys.path`（它内部那几个
    脚本互相是平铺 import 的），所以这里不用再插一次。
    """
    from scripts.module_probe import pipeline  # noqa: PLC0415

    return pipeline


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """一次导入的结局。**字段全是数量与类别**——剧透约束，见 `ModuleImportJob`。"""

    ok: bool
    scenario_id: str | None = None
    failure_reason: str = ""
    failure_kinds: tuple[str, ...] = ()


async def run_import_job(
    job_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    work_root: Path,
) -> ImportOutcome:
    """跑完一个 job，把过程与结局写回它自己那一行。

    🔴 **任何异常都要落成 `failed` + 一条可执行的理由**，绝不让 job 挂在
    `running` 上——那样用户看到的就是永久转圈，而这正是 job 存在要解决的事。
    """
    loop = asyncio.get_running_loop()
    pipeline = _load_pipeline()

    async with session_factory() as db:
        job = await db.get(ModuleImportJob, job_id)
        if job is None or job.source_path is None:
            raise LookupError(f"job 不存在或没有上传件：{job_id}")
        source = Path(job.source_path)
        job.status = STATUS_RUNNING
        await db.commit()

    async def _set_stage(stage: str) -> None:
        async with session_factory() as db:
            row = await db.get(ModuleImportJob, job_id)
            if row is not None:
                row.stage = stage
                await db.commit()

    def _on_stage(stage: str) -> None:
        """在工作线程里被调用——调度回主循环，不在这里碰 db。"""
        asyncio.run_coroutine_threadsafe(_set_stage(stage), loop)

    work_dir = work_root / job_id
    out_structured = work_dir / "structured.json"
    try:
        result = await asyncio.to_thread(
            pipeline.convert,
            source,
            work_dir=work_dir,
            out_structured=out_structured,
            on_stage=_on_stage,
        )
    except pipeline.ConversionError as exc:
        # 管线自己给的理由已经是"可执行"的（取文层会说清该换成什么格式）。
        return await _finish_failed(job_id, str(exc), (), session_factory, work_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("module_import_crashed", job_id=job_id, error=type(exc).__name__)
        # 🔴 只报异常类型名，不报 str(exc)——异常消息里可能带着文件内容片段。
        return await _finish_failed(
            job_id,
            "转换过程出错了，请重试；多次失败请换一份文件。",
            (),
            session_factory,
            work_dir,
        )

    if not result.ok:
        report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
        errors = report.get("report", {}).get("all_errors") or []
        kinds = tuple(normalize_failure_kinds(errors))
        return await _finish_failed(
            job_id, result.failure_reason, kinds, session_factory, work_dir, result=result
        )

    outcome = await _register(job_id, out_structured, session_factory, result=result)
    _cleanup(work_dir)
    return outcome


def _cleanup(work_dir: Path) -> None:
    """删中间产物；`MODULE_IMPORT_KEEP_WORK=1` 时留下来。

    🔴 留现场的理由：失败的中间产物是唯一能回答"自修跑没跑、修了什么"的东西，
    而删掉之后只能靠再花一次 ¥0.35 重现——**且结果未必一样**（实测同一份 PDF
    四次跑出三种不同的失败）。默认仍然删：那些文件含第三方正文。
    """
    if get_settings().module_import_keep_work:
        logger.info("module_import_work_kept", path=str(work_dir))
        return
    shutil.rmtree(work_dir, ignore_errors=True)


async def _finish_failed(
    job_id: str,
    reason: str,
    kinds: tuple[str, ...],
    session_factory: async_sessionmaker[AsyncSession],
    work_dir: Path,
    *,
    result: Any = None,
) -> ImportOutcome:
    async with session_factory() as db:
        job = await db.get(ModuleImportJob, job_id)
        if job is not None:
            job.status = STATUS_FAILED
            job.error_message = reason
            job.failure_kinds = list(kinds)
            job.finished_at = datetime.now(UTC)
            _copy_counts(job, result)
            await db.commit()
    _cleanup(work_dir)
    return ImportOutcome(ok=False, failure_reason=reason, failure_kinds=kinds)


def _copy_counts(job: ModuleImportJob, result: Any) -> None:
    """把管线量到的数字抄进 job。**只有数量，没有内容。**"""
    if result is None:
        return
    job.page_count = result.page_count
    job.image_count = result.image_count
    job.char_count = result.chars
    job.item_count = result.items
    job.hard_failure_count = result.hard_failures


async def _register(
    job_id: str,
    structured_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    result: Any,
) -> ImportOutcome:
    """把产物注册成一个可玩的 scenario。

    🔴 **每次成功导入产生一条新 scenario，永不原地更新**（`ImportedModule` 的
    不变量）：重跑会产出不同的 structured，原地改会把正在玩的房间的世界换掉。
    """
    raw = json.loads(structured_path.read_text(encoding="utf-8"))
    module = ScenarioModule.model_validate(raw)
    scenario_id = str(uuid.uuid4())

    async with session_factory() as db:
        job = await db.get(ModuleImportJob, job_id)
        if job is None:
            raise LookupError(f"job 不存在：{job_id}")
        db.add(
            Scenario(
                id=scenario_id,
                game_system_id=BUILTIN_SYSTEM_ID,
                owner_user_id=job.owner_user_id,
                title=module.meta.title or (job.source_filename or "导入的模组"),
                players_min=1,
                players_max=6,
                # 🔴 synopsis 留空：它是**玩家可见**的目录简介，而我们没有任何
                # 机械手段确认模组的哪一段是无剧透的。宁可空着，不猜。
                synopsis=None,
            )
        )
        await db.flush()
        db.add(ImportedModule(scenario_id=scenario_id, structured=raw))

        job.status = STATUS_SUCCEEDED
        job.stage = "registering"
        job.result_scenario_id = scenario_id
        job.finished_at = datetime.now(UTC)
        job.node_count = len(module.nodes)
        job.npc_count = len(module.npcs)
        job.ending_count = len(module.endings)
        job.agenda_count = len(module.agenda)
        _copy_counts(job, result)
        await db.commit()

    return ImportOutcome(ok=True, scenario_id=scenario_id)
