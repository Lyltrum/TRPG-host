"""Example 的 pydantic 请求/响应模型。

跟 models/example.py 的 SQLAlchemy ORM 模型是两回事：ORM 模型描述"数据库里
这张表长什么样"，这里的 pydantic 模型描述"HTTP 接口的请求体/响应体长什么样"——
两者字段大部分重叠，但职责不同（比如 id/created_at/updated_at 是服务端生成的，
不应该出现在"新建"请求体里；ORM 模型上有的字段也不一定都要暴露给外部）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExampleCreate(BaseModel):
    """POST /api/v1/examples 的请求体。

    Field(min_length=1, ...) 这些约束由 pydantic 在进入路由函数之前自动校验，
    校验不过会被 FastAPI 转成 422，再经 main.py 里的 RequestValidationError
    处理器包装成统一响应体——业务代码里完全不用手写"名称不能为空"这种判断。
    """

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ExampleUpdate(BaseModel):
    """PUT /api/v1/examples/{id} 的请求体，字段跟 Create 一样（全量更新）。"""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ExampleRead(BaseModel):
    """对外返回时的形状，会被包在 ApiResponse[ExampleRead] 里。"""

    # from_attributes=True：允许直接从 ORM 对象（models.Example 实例）构造，
    # 也就是 controller/v1/examples.py 里那句 `ExampleRead.model_validate(example)`——
    # 不用手动把 ORM 对象的每个字段一个个搬到 dict 里再构造。
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
