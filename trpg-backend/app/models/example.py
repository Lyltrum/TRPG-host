"""Example 的数据库表结构（ORM 模型）。

这是一个和真实业务完全无关的示例表，只用来演示"从数据库到接口"的完整链路
该怎么搭：ORM 模型（这个文件，model 层）→ pydantic 校验/序列化（dto/example.py，
dto 层）→ 数据访问函数（service/example.py，service 层）→ HTTP 路由
（controller/v1/examples.py，controller 层）。以后加真实业务表时，
照这四层的分工抄一份新的就行。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Example(Base):
    __tablename__ = "examples"

    # 主键用应用层生成的 UUIDv4 字符串，而不是数据库自增整数或数据库端的
    # UUID 生成函数：一是 SQLite 和 PostgreSQL 对"数据库端生成 UUID"的支持
    # 方式不一样，用应用层生成能保证两边行为一致；二是自增整数会暴露"一共有
    # 多少条记录""这条是第几个创建的"这类信息，UUID 没有这个问题。
    # default=... 是兜底：只有在没有显式传 id 的时候才会生效（本项目的
    # service/example.py 里是显式传的，这里的 default 是留给"以后有别的代码
    # 路径不小心忘记传 id"的安全网）。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # unique=True + index=True：数据库层面强制名称不重复，并建索引加速按名称查询。
    # 光靠应用层判断"是否已存在同名记录"（service.get_example_by_name）在并发请求下
    # 可能有竞态（两个请求同时查到"不存在"然后都去插入），数据库唯一约束是最后一道防线，
    # 真出现竞态时会在插入阶段抛 IntegrityError，而不是允许出现两条同名记录。
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    description: Mapped[str | None] = mapped_column(String(2000), default=None)

    # created_at/updated_at 用 Python 端的 default/onupdate（在 flush 时由
    # SQLAlchemy 计算好值再发 INSERT/UPDATE），而不是数据库端的 server_default/
    # DEFAULT CURRENT_TIMESTAMP：这样插入/更新之后立刻就能从 Python 对象上
    # 读到这两个字段的值，不需要额外 `await session.refresh(obj)` 从数据库
    # 重新查一次——SQLite 和 PostgreSQL 两边行为完全一致，逻辑更简单。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),  # 每次 UPDATE 这一行时自动刷新
    )
