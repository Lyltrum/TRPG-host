"""`/api/v1/examples` 的端到端测试：覆盖完整 CRUD 流程，以及 404/409/422 三种
错误路径（对应"记录不存在"/"名称冲突"/"请求体校验失败"）。

每个测试函数都会用到 conftest.py 里的 `client` fixture——每次运行都是全新的
内存数据库，测试之间互不干扰，不需要手动清理数据。
"""

from httpx import AsyncClient

BASE = "/api/v1/examples"


async def test_list_examples_starts_empty(client: AsyncClient) -> None:
    """还没有任何数据时，列表接口应该返回空数组，而不是报错。"""
    response = await client.get(BASE)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []
    assert body["error"] is None


async def test_create_get_update_delete_flow(client: AsyncClient) -> None:
    """走一遍完整的 增 → 查列表 → 查单个 → 改 → 删 → 确认已删除。"""
    create_res = await client.post(BASE, json={"name": "线索A", "description": "第一条线索"})
    assert create_res.status_code == 201
    created = create_res.json()["data"]
    assert created["name"] == "线索A"
    assert created["description"] == "第一条线索"
    # id/created_at/updated_at 是服务端生成的，这里只断言"有值"，不关心具体值。
    assert created["id"]
    assert created["created_at"]
    assert created["updated_at"]
    example_id = created["id"]

    list_res = await client.get(BASE)
    assert list_res.status_code == 200
    assert len(list_res.json()["data"]) == 1

    get_res = await client.get(f"{BASE}/{example_id}")
    assert get_res.status_code == 200
    assert get_res.json()["data"]["id"] == example_id

    # 更新时把 description 传 None，验证"可选字段可以被清空"这条路径也是通的。
    update_res = await client.put(
        f"{BASE}/{example_id}", json={"name": "线索A-已更新", "description": None}
    )
    assert update_res.status_code == 200
    updated = update_res.json()["data"]
    assert updated["name"] == "线索A-已更新"
    assert updated["description"] is None

    delete_res = await client.delete(f"{BASE}/{example_id}")
    assert delete_res.status_code == 200
    # 删除成功时 data 是 null，但响应信封本身仍然是完整的三个字段。
    assert delete_res.json() == {"success": True, "data": None, "error": None}

    # 删除之后再查，应该变成 404，而不是返回一个"空的"记录。
    get_after_delete = await client.get(f"{BASE}/{example_id}")
    assert get_after_delete.status_code == 404
    assert get_after_delete.json()["error"]["code"] == "NOT_FOUND"


async def test_get_missing_example_returns_404(client: AsyncClient) -> None:
    """查一个根本不存在的 id，应该是 404 + NOT_FOUND，而不是 500 或者返回空数据。"""
    response = await client.get(f"{BASE}/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"] == {"code": "NOT_FOUND", "message": "示例不存在"}


async def test_create_duplicate_name_returns_409(client: AsyncClient) -> None:
    """名称唯一性约束：第一次创建成功，同名的第二次创建应该被拒绝为 409。"""
    payload = {"name": "重复名称", "description": None}
    first = await client.post(BASE, json=payload)
    assert first.status_code == 201

    second = await client.post(BASE, json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


async def test_create_with_invalid_payload_returns_422(client: AsyncClient) -> None:
    """name 不满足 min_length=1 的校验规则时，pydantic 应该在进入路由函数之前
    就拦下来，统一转成 422 + VALIDATION_ERROR。"""
    response = await client.post(BASE, json={"name": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_update_missing_example_returns_404(client: AsyncClient) -> None:
    """更新一个不存在的 id，同样应该是 404，而不是"静默创建"或者报错崩溃。"""
    response = await client.put(
        f"{BASE}/does-not-exist", json={"name": "任意名称", "description": None}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
