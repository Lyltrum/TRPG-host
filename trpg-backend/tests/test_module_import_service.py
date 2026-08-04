"""导入 service：上传闸门、哈希去重、重跑（`exec/29` 第 5 步）。

这里**不跑真管线**（一次 71 次 LLM 调用 / 5–26 分钟），只验不需要模型就能定的
那几条——而它们恰好是最容易悄悄坏掉的：

- **拒绝理由可执行**：「失败的唯一出口是拒绝」的前提是那条理由告诉用户下一步
  做什么。`.zip` 尤其——它是最常见的误传，混进"未知格式"就等于没提示。
- **去重只认成功**：失败过的必须允许重试（组装输出不稳定，拒绝率有运气成分）。
- **重跑新建 job**：旧 job 的失败理由要留着，否则点三次就再也不知道前两次为什么
  失败。
- **DTO 不漏 `source_path`**：那是服务器上第三方正文的路径。
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.errors import AppException
from app.core.module_import.job_state import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)
from app.models.replay import ModuleImportJob
from app.service import module_import as svc

_db_path = Path(tempfile.mkdtemp(prefix="trpg-import-svc-test-")) / "svc.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
def _never_spawn(monkeypatch: pytest.MonkeyPatch):
    """🔴 测试绝不许起真任务——那会去连 DeepSeek。"""
    spawned: list[str] = []
    monkeypatch.setattr(svc, "_spawn", lambda job_id, _factory: spawned.append(job_id))
    return spawned


# ── 上传闸门 ──────────────────────────────────────────


def test_archive_gets_its_own_actionable_reason() -> None:
    """🔴 压缩包是最常见的误传，理由必须告诉用户挑出哪一份文件。

    只支持一个文件是定下来的（`exec/29 §7.2 ③`），所以这条提示是这个决定
    唯一对用户可见的地方——写成"不支持的文件类型 .zip"等于没说。
    """
    with pytest.raises(AppException) as exc:
        svc.reject_unsupported("某模组.zip")

    assert "压缩包" in exc.value.message
    assert "挑出模组正文" in exc.value.message


@pytest.mark.parametrize("name", ["a.epub", "b.mp4", "c", "d.docx.bak"])
def test_unknown_types_are_rejected_with_the_supported_list(name: str) -> None:
    with pytest.raises(AppException) as exc:
        svc.reject_unsupported(name)

    assert ".pdf" in exc.value.message, "拒绝时要顺带说清支持什么"


@pytest.mark.parametrize("name", ["m.pdf", "m.PDF", "m.docx", "m.doc", "m.txt"])
def test_supported_types_pass(name: str) -> None:
    svc.reject_unsupported(name)


def test_extension_gate_runs_before_anything_is_stored() -> None:
    """闸门是个纯函数——它不碰 db、不碰磁盘，所以一定在落盘之前。

    这条守的是"不为读不了的文件占磁盘、更不为它开 job"。
    """
    import inspect

    src = inspect.getsource(svc.start_import)
    assert src.index("reject_unsupported") < src.index("_read_upload")


# ── DTO 不漏内部字段 ──────────────────────────────────


async def test_dto_never_carries_the_stored_file_path() -> None:
    """🔴 `source_path` 指向服务器上的第三方模组正文，绝不能跨到前端。"""
    async with _session_factory() as db:
        job = await _job(
            db,
            status=STATUS_SUCCEEDED,
            source_filename="m.pdf",
            source_path="/var/aidm/uploads/deadbeef.pdf",
            source_sha256="deadbeef",
        )

    dumped = svc._to_dto(job).model_dump()

    assert "/var/aidm" not in str(dumped)
    assert not any("path" in k.lower() for k in dumped)
    assert not any("sha" in k.lower() for k in dumped), "内容哈希也没必要给前端"


# ── 去重 ──────────────────────────────────────────────


async def _job(db, **kw) -> ModuleImportJob:
    row = ModuleImportJob(
        id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        **kw,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _upload(filename: str, payload: bytes) -> UploadFile:
    """真的 `UploadFile`，不是替身——顺带把分块读那段也真跑一遍。"""
    return UploadFile(file=BytesIO(payload), filename=filename)


async def test_a_successful_import_of_the_same_file_is_reused(tmp_path, monkeypatch) -> None:
    """同一份文件转过就别再付一次钱（¥0.35 / 71 次调用）。"""
    monkeypatch.setattr(svc, "_import_root", lambda: tmp_path)
    import hashlib

    payload = b"module text"
    digest = hashlib.sha256(payload).hexdigest()

    async with _session_factory() as db:
        old = await _job(db, status=STATUS_SUCCEEDED, source_sha256=digest, source_filename="m.txt")

        dto = await svc.start_import(
            db, _upload("m.txt", payload), user_id=None, session_factory=_session_factory
        )

        assert dto.job_id == old.id
        assert len((await db.scalars(select(ModuleImportJob))).all()) == 1, "不该多建一行"


async def test_a_failed_import_of_the_same_file_is_retried_not_reused(tmp_path, monkeypatch):
    """🔴 只认成功。

    组装输出高度不稳定——同一批中间产物跑两次问题类别完全不同——所以"上次失败
    了所以这次也别试"是错的，重跑确实有救回来的机会。
    """
    monkeypatch.setattr(svc, "_import_root", lambda: tmp_path)
    import hashlib

    payload = b"module text"
    digest = hashlib.sha256(payload).hexdigest()

    async with _session_factory() as db:
        old = await _job(db, status=STATUS_FAILED, source_sha256=digest, source_filename="m.txt")

        dto = await svc.start_import(
            db, _upload("m.txt", payload), user_id=None, session_factory=_session_factory
        )

        assert dto.job_id != old.id
        assert len((await db.scalars(select(ModuleImportJob))).all()) == 2


# ── 重跑 ──────────────────────────────────────────────


async def test_retry_creates_a_new_job_and_keeps_the_old_one(tmp_path) -> None:
    """🔴 旧 job 原样留着——否则用户点三次就再也不知道前两次为什么失败。"""
    src = tmp_path / "m.txt"
    src.write_text("x")

    async with _session_factory() as db:
        old = await _job(
            db,
            status=STATUS_FAILED,
            source_filename="m.txt",
            source_path=str(src),
            source_sha256="abc",
            error_message="校验未通过：3 处问题（numeric、skill）",
        )

        dto = await svc.retry_import(db, old.id, session_factory=_session_factory)

        assert dto.job_id != old.id
        assert dto.retried_from_job_id == old.id
        refreshed = await db.get(ModuleImportJob, old.id)
        assert refreshed is not None
        assert refreshed.status == STATUS_FAILED
        assert refreshed.error_message == "校验未通过：3 处问题（numeric、skill）"


async def test_interrupted_jobs_can_be_retried(tmp_path) -> None:
    """被重启冲掉的当然要能重来——这正是它跟 failed 分开的意义。"""
    src = tmp_path / "m.txt"
    src.write_text("x")

    async with _session_factory() as db:
        old = await _job(
            db, status=STATUS_INTERRUPTED, source_path=str(src), source_filename="m.txt"
        )

        dto = await svc.retry_import(db, old.id, session_factory=_session_factory)

        assert dto.retried_from_job_id == old.id


async def test_retrying_a_succeeded_job_is_refused(tmp_path) -> None:
    src = tmp_path / "m.txt"
    src.write_text("x")

    async with _session_factory() as db:
        old = await _job(db, status=STATUS_SUCCEEDED, source_path=str(src))

        with pytest.raises(AppException) as exc:
            await svc.retry_import(db, old.id, session_factory=_session_factory)

    assert "已经导入成功" in exc.value.message


async def test_retrying_a_running_job_is_refused(tmp_path) -> None:
    """否则同一份文件会被同时转两遍，钱翻倍。"""
    src = tmp_path / "m.txt"
    src.write_text("x")

    async with _session_factory() as db:
        old = await _job(db, status=STATUS_RUNNING, source_path=str(src))

        with pytest.raises(AppException) as exc:
            await svc.retry_import(db, old.id, session_factory=_session_factory)

    assert "还在跑" in exc.value.message


async def test_retry_without_the_original_file_says_so(tmp_path) -> None:
    """🔴 上传件被清掉时要**明说重新上传**，不是静默失败也不是假装能重跑。"""
    async with _session_factory() as db:
        old = await _job(db, status=STATUS_FAILED, source_path=str(tmp_path / "没了.txt"))

        with pytest.raises(AppException) as exc:
            await svc.retry_import(db, old.id, session_factory=_session_factory)

    assert "重新上传" in exc.value.message


# ── 并发闸门 ──────────────────────────────────────────


async def test_concurrency_is_capped(tmp_path, monkeypatch) -> None:
    """一次导入 ≈ 71 次调用，没有上限几个人同时传就把 provider 排满。"""
    monkeypatch.setattr(svc, "_import_root", lambda: tmp_path)
    from app.core.config import get_settings

    limit = get_settings().module_import_max_concurrent

    async with _session_factory() as db:
        for _ in range(limit):
            await _job(db, status=STATUS_RUNNING)

        with pytest.raises(AppException) as exc:
            await svc.start_import(
                db, _upload("m.txt", b"x"), user_id=None, session_factory=_session_factory
            )

    assert "排满" in exc.value.message
