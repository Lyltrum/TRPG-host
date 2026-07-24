# 执行规格 · 路线 5 Visibility 运行时 + 路线 6 导演层（心跳 / 阶段 / 结局）

> 设计依据：[03](../03-模组抽象契约.md) 第四节 V 预测；[05](../05-导演层-心跳议程与对局阶段.md)
> 提案②③；4b 已长出 `visibility_pairs` 静态骨架（[07](07-4b-schema长出实体图与密级配对-执行规格.md)）。
> 分支 `feat/keeper-agent`。

## 0. 边界

### 本期做

| 项 | 行为 |
|---|---|
| **5 · 揭开记账** | 裁决字段 `visibility_revealed[]`；代码写 `keeper_state["已揭开配对"]`；局面注入「密级配对状态」 |
| **5 · 保留键** | 系统键禁止 `state_updates` 改写 |
| **6 · 阶段** | `对局阶段`：opening→investigation→finished；首次 narrate 有 opening 则初始化 opening |
| **6 · 结局** | `ending_reached` → 写结局 id + finished；再行动回「本局已结束」 |
| **6 · 心跳** | 全局 ticker；默认 **关**；沉默/无 pending/有 WS/锁空闲/节流后主动 narrate |

### 本期不做

- ❌ 真 per-player 私信通道（叙事仍全员广播；观察者字段只预留）
- ❌ SDK / 前端 / DTO 协议变更（心跳复用 `narration.push`）
- ❌ alembic 新列
- ❌ 完整 V 函数（随 T 演化的任意事实子集）——只覆盖 **配对表揭开**

## 1. 关键实现落点

- `app/core/keeper/visibility.py` — 解析/序列化/格式化
- `app/core/keeper/phase.py` — 阶段常量与文案
- `app/core/keeper/decision.py` — 新字段 + execute 副作用
- `app/core/keeper/tools.py` — mark_visibility / set_phase / 保留键
- `app/core/keeper/agent.py` — 注入局面、finished 守卫、opening 初始化、心跳丢检定
- `app/core/keeper/heartbeat.py` + `config` + `main.lifespan`
- `app/service/ws_manager.py` — `connected_room_ids` / `has_connections`
- 测试：`tests/test_keeper_visibility_phase.py`

## 2. 配置

```
KEEPER_HEARTBEAT_ENABLED=false   # 默认关
KEEPER_HEARTBEAT_SILENCE_SECONDS=100
KEEPER_HEARTBEAT_MIN_INTERVAL_SECONDS=300
KEEPER_HEARTBEAT_SCAN_INTERVAL_SECONDS=30
KEEPER_HEARTBEAT_MAX_CONSECUTIVE=2
```

## 3. 验证

```
cd trpg-backend
.venv/bin/ruff check app/core/keeper app/main.py app/service/ws_manager.py
.venv/bin/pytest tests/test_keeper_visibility_phase.py tests/test_keeper_agenda.py tests/test_keeper_module_schema_4b.py -q
```

## 4. 提交

```
feat(keeper): 路线5/6——密级揭开记账、对局阶段与世界心跳
```

不 push 除非用户要求。不碰 main/upstream。
