"""Service 层：模组导入（`exec/29` 第 5 步）。

协议位是骨架期（issue #77）就占好的两个端点，这里把 `NOT_IMPLEMENTED` 换成
真实现，端点形状不变——只是 `POST /modules/import` 从 JSON 体变成 multipart
上传（全项目第一个 `UploadFile`）。

## 这一层负责的四件事

1. **收文件**：大小上限 + 扩展名白名单，**在落盘之前拦**。
2. **哈希去重**：同一份文件转过就复用，别再付一次钱（¥0.35 / 71 次调用）。
3. **并发闸门**：一次导入 ≈ 71 次调用，没有上限的话几个人同时上传就把 provider
   排满、把本进程拖垮。
4. **重跑**：**新建一个 job**，不复活旧的——旧 job 的失败理由要留着，否则用户
   点三次就再也不知道前两次为什么失败（`exec/29 §7.2 ②`）。

## 🔴 拒绝理由必须可执行

「失败的唯一出口是拒绝」的前提是那条理由**告诉用户下一步做什么**。这里每一条
`AppException` 的文案都按这个写，`.zip` 那条尤其——它是最常见的误传。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.errors import AppException, ErrorCode
from app.core.module_import.job_state import (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)
from app.core.module_import.runner import run_import_job
from app.dto.module import ModuleImportJobRead
from app.models.replay import ModuleImportJob

logger = structlog.get_logger()

#: 上传上限。实测最大的一份模组 ~98 KB 文本 / 24 页 PDF；32 MB 给扫描件留了
#: 足够余量，同时挡住"传错文件"（视频、整套资料包）。
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: 取文层支持的格式。**白名单，不是黑名单**——未知格式一律拒。
ALLOWED_SUFFIXES = (".pdf", ".docx", ".doc", ".txt")

#: 压缩包是最常见的误传：网上下的模组常打包成 zip/rar。它需要一条**专门的**
#: 理由，不能混进"未知格式"（`exec/29 §7.2 ③`：只支持一个文件）。
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".gz")

#: 后台任务的强引用。`asyncio.create_task` 只持弱引用，不存住会被 GC 掉。
_running: set[asyncio.Task] = set()


def _import_root() -> Path:
    settings = get_settings()
    if settings.module_import_dir:
        root = Path(settings.module_import_dir).expanduser()
    else:
        root = Path.home() / ".aidm" / "module-import"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _to_dto(job: ModuleImportJob) -> ModuleImportJobRead:
    """🔴 逐字段显式构造，**不用 `from_attributes`**。

    自动映射会把以后新加的列一起带出去——而这张表里有 `source_path`（服务器上
    第三方正文的路径）。剧透约束不能依赖"下一个加字段的人记得排除它"。
    """
    return ModuleImportJobRead(
        job_id=job.id,
        status=job.status,
        stage=job.stage,
        source_filename=job.source_filename,
        result_scenario_id=job.result_scenario_id,
        error_message=job.error_message,
        failure_kinds=list(job.failure_kinds or []),
        page_count=job.page_count,
        image_count=job.image_count,
        char_count=job.char_count,
        item_count=job.item_count,
        node_count=job.node_count,
        npc_count=job.npc_count,
        ending_count=job.ending_count,
        agenda_count=job.agenda_count,
        hard_failure_count=job.hard_failure_count,
        retried_from_job_id=job.retried_from_job_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )


def reject_unsupported(filename: str) -> None:
    """扩展名闸门。**在落盘之前**——不为读不了的文件占磁盘、更不为它开 job。"""
    suffix = Path(filename).suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "请上传模组正文本身（PDF / DOCX / DOC / TXT），压缩包暂不支持。"
            "如果包里有多个文件，挑出模组正文那一份再传。",
            status.HTTP_400_BAD_REQUEST,
        )
    if suffix not in ALLOWED_SUFFIXES:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            f"不支持的文件类型 {suffix or '（没有扩展名）'}。支持：{'、'.join(ALLOWED_SUFFIXES)}。",
            status.HTTP_400_BAD_REQUEST,
        )


async def _read_upload(upload: UploadFile) -> bytes:
    """🔴 边读边数，超限当场断——不能先全读进内存再判断大小。"""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"文件太大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）。"
                "如果是扫描版 PDF，目前也处理不了，请找文字版。",
                status.HTTP_400_BAD_REQUEST,
            )
        chunks.append(chunk)
    if total == 0:
        raise AppException(ErrorCode.VALIDATION_ERROR, "文件是空的。", status.HTTP_400_BAD_REQUEST)
    return b"".join(chunks)


async def _guard_concurrency(db: AsyncSession) -> None:
    limit = get_settings().module_import_max_concurrent
    active = await db.scalar(
        select(func.count())
        .select_from(ModuleImportJob)
        .where(ModuleImportJob.status.in_((STATUS_PENDING, STATUS_RUNNING)))
    )
    if (active or 0) >= limit:
        raise AppException(
            ErrorCode.CONFLICT,
            f"同时最多转换 {limit} 个模组，现在排满了。等前一个转完再试。",
            status.HTTP_409_CONFLICT,
        )


async def start_import(
    db: AsyncSession,
    upload: UploadFile,
    *,
    user_id: str | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> ModuleImportJobRead:
    filename = (upload.filename or "").strip()
    if not filename:
        raise AppException(ErrorCode.VALIDATION_ERROR, "没有文件名。", status.HTTP_400_BAD_REQUEST)
    reject_unsupported(filename)
    await _guard_concurrency(db)

    payload = await _read_upload(upload)
    digest = hashlib.sha256(payload).hexdigest()

    # 去重：同一份文件已经成功转过就直接把那次的 job 还回去，不再付一次钱。
    # 🔴 只认 succeeded——失败过的要允许重试（拒绝率有运气成分）。
    done = (
        await db.scalars(
            select(ModuleImportJob)
            .where(
                ModuleImportJob.source_sha256 == digest,
                ModuleImportJob.status == STATUS_SUCCEEDED,
            )
            .order_by(ModuleImportJob.created_at.desc())
            .limit(1)
        )
    ).first()
    if done is not None:
        logger.info("module_import_deduped", job_id=done.id)
        return _to_dto(done)

    job_id = str(uuid.uuid4())
    stored = _import_root() / "uploads" / f"{digest}{Path(filename).suffix.lower()}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)

    job = ModuleImportJob(
        id=job_id,
        owner_user_id=user_id,
        status=STATUS_PENDING,
        stage="received",
        source_filename=filename[:255],
        source_sha256=digest,
        source_path=str(stored),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    _spawn(job_id, session_factory)
    return _to_dto(job)


def _spawn(job_id: str, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """起后台任务。进程内跑（`exec/29 §7.2 ①`）——重启会丢，由启动清扫兜底。"""
    task = asyncio.create_task(
        run_import_job(job_id, session_factory=session_factory, work_root=_import_root() / "work")
    )
    _running.add(task)
    task.add_done_callback(_running.discard)


async def list_import_jobs(db: AsyncSession, *, user_id: str) -> list[ModuleImportJobRead]:
    """我的导入记录，最近的在前。

    「我的模组」那一屏要同时显示**正在转的、转好的、没转成的**——正在转的那条
    就是"关掉页面之后怎么回来"的答案，所以这个接口不能只返回终态。

    🔴 按 `owner_user_id` 过滤：别人导入的模组连文件名都不该露出去。
    """
    rows = await db.scalars(
        select(ModuleImportJob)
        .where(ModuleImportJob.owner_user_id == user_id)
        .order_by(ModuleImportJob.created_at.desc())
    )
    return [_to_dto(row) for row in rows]


async def get_import_job(db: AsyncSession, job_id: str, user_id: str) -> ModuleImportJobRead:
    """GET /api/v1/modules/import/{jobId} —— 轮询导入任务状态。

    🔴 **这个端点此前没有任何鉴权**（2026-08-19 补）：连登录都不要。它泄的不是
    模组内容（DTO 那层挡住了），而是 `source_filename`——用户自己的文件名，
    以及"某人导入过一份东西"这件事本身。

    `list_modules` 与 `get_module_detail` 都已经按主人过滤，**只有这一头漏了**：
    「一份数据有几个出口，规则就要落几处」。

    看不到的一律当**不存在**（不是 403），跟 `get_module_detail` 同口径——
    不确认"这个 id 存在但你没权限"。
    """
    job = await db.get(ModuleImportJob, job_id)
    if job is None or job.owner_user_id != user_id:
        raise AppException(ErrorCode.NOT_FOUND, "导入任务不存在", status.HTTP_404_NOT_FOUND)
    return _to_dto(job)


async def retry_import(
    db: AsyncSession,
    job_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> ModuleImportJobRead:
    """重跑。**新建一个 job，不复活旧的。**

    组装输出高度不稳定——同一批中间产物跑两次问题类别完全不同——所以重跑确实
    有用；但**不自动**（那等于默默再花一次钱），由用户点。旧 job 原样留着，
    否则点三次就再也不知道前两次为什么失败。
    """
    old = await db.get(ModuleImportJob, job_id)
    if old is None:
        raise AppException(ErrorCode.NOT_FOUND, "导入任务不存在", status.HTTP_404_NOT_FOUND)
    if old.status == STATUS_SUCCEEDED:
        raise AppException(
            ErrorCode.CONFLICT, "这个模组已经导入成功了，不用重试。", status.HTTP_409_CONFLICT
        )
    if old.status in (STATUS_PENDING, STATUS_RUNNING):
        raise AppException(
            ErrorCode.CONFLICT, "这个任务还在跑，等它结束再说。", status.HTTP_409_CONFLICT
        )
    if not old.source_path or not Path(old.source_path).exists():
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "原文件已经不在服务器上了，请重新上传。",
            status.HTTP_400_BAD_REQUEST,
        )
    await _guard_concurrency(db)

    job_id_new = str(uuid.uuid4())
    job = ModuleImportJob(
        id=job_id_new,
        owner_user_id=old.owner_user_id,
        status=STATUS_PENDING,
        stage="received",
        source_filename=old.source_filename,
        source_sha256=old.source_sha256,
        source_path=old.source_path,
        retried_from_job_id=old.id,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    _spawn(job_id_new, session_factory)
    return _to_dto(job)
