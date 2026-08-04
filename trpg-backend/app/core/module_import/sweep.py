"""启动清扫：把上一次进程留下的 `running` job 显式作废（`exec/29 §7.2 ①`）。

## 🔴 为什么不用心跳、不用租约

导入 job 跑在**进程内的 `asyncio` 任务**里（决策 ①：独立 worker 要新增部署
单元 + 队列依赖，而后端现在是单个 uvicorn 进程，收益要等并发量上来才有）。
代价是后端一重启，正在跑的 job 就没有任何进程在跑它了。

而**因为只有一个进程**，「启动那一刻还挂着 `running` 的 job」**恰好等价于**
「它的进程已经没了」——这个判断是精确的，不是启发式的。所以不需要心跳、不
需要租约，一次 UPDATE 就够。

🔴 **这条正确性的前提是"只有一个进程"。** 哪天上多 worker，这个清扫会把别的
worker 正在跑的 job 误杀。前提写在这里；**不**顺手加个"以防万一"的租约——
那是为不可能场景写的错误处理，而且它会让上面那句"精确"变成"大概"。

## 🔴 作废不等于失败，更不等于重跑

- 状态是 `interrupted`，**不是** `failed`：这不是模组的问题，混在一起会让用户
  以为自己的模组转不了。
- **绝不自动重跑**：重启后默默再花一次钱（¥0.35 / 71 次调用）是最坏的结果。
  重跑由用户点按钮（决策 ②）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_import.job_state import (
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    stale_running_reason,
)
from app.models.replay import ModuleImportJob

logger = structlog.get_logger()


async def sweep_stale_jobs(db: AsyncSession) -> int:
    """把所有 `running` / `pending` 的 job 标成 `interrupted`。返回条数。

    `pending` 也要扫：它是"已收下文件、还没开始跑"，同样只活在进程内存里的
    调度中——进程没了它永远不会被捡起来，**留在 pending 就是永久转圈**。
    """
    stale = (
        await db.scalars(
            select(ModuleImportJob.id).where(
                ModuleImportJob.status.in_((STATUS_RUNNING, STATUS_PENDING))
            )
        )
    ).all()
    if not stale:
        return 0

    await db.execute(
        update(ModuleImportJob)
        .where(ModuleImportJob.id.in_(stale))
        .values(
            status=STATUS_INTERRUPTED,
            error_message=stale_running_reason(),
            finished_at=datetime.now(UTC),
        )
    )
    await db.commit()
    # 只记条数——job id 是安全的，但没必要；模组相关的一切都不进日志。
    logger.info("module_import_jobs_swept", count=len(stale))
    return len(stale)
