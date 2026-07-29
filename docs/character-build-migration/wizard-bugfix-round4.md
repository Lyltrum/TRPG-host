# 建卡向导 bug 修复第四轮 · 方案 A（exec/12 #18 + #20）

> 2026-07-29，Opus 复审 round2 时发现两个**既有**的结构性问题，用户已经
> 拍板走「方案 A」。本轮同时解决：
> - **#18**：年龄修正会被重复套用，属性被反复扣减；
> - **#20**（阻塞级）：掷点池角色只要年龄修正真的改动了属性，就再也无法
>   完成建卡。
>
> 两条是**同一个结构性问题的两面**，必须一起改，分开做会互相打架。

## 🔴 结构性根因：一份数据被迫扮演两个角色

建卡过程里其实存在**两份**语义不同的属性：

| | 含义 | 谁该用它 |
|---|---|---|
| **分配值**（allocation） | 玩家在属性步骤分配出来的原始点数 | 点数预算 / 掷点池总和 / 步进为 5 这三条校验；年龄修正的计算基准 |
| **有效值**（effective） | 年龄修正之后的最终属性 | 衍生值（HP/SAN/MP/DB/MOV）、技能基础值（如闪避 `DEX/2`）、职业技能点公式（如 `EDU*2+APP*2`）、角色卡上真正显示的数字 |

现在这两份被压进了同一个 `character.attributes` / `state.attr`，于是：

- **#20**：年龄修正后的有效值被拿去跑"分配值"才该满足的校验 → 必然失败。
  实测：45 岁修正后 `INVALID_ATTRIBUTES ×3`（"CON 的值 58 必须是 5 的
  倍数"）；构造仅改总和的场景 → `ATTRIBUTE_POOL_MISMATCH`（"属性点总数
  455 与掷出的点数池总值 465 不一致"）。40 岁以上 100% 触发，20–39 岁段
  只要 EDU 改进检定成功（EDU=60 时约 40%）也触发。
- **#18**：年龄修正把结果写回同一个字段，下次套用就在**已经扣过**的基础
  上再扣一次。实测 45 岁套两次：STR+CON+DEX 共扣 10（规则规定 5）、
  APP 扣 10（规定 5）、EDU 从 60 虚高到 67。而前端做不到"只调用一次"——
  改任何属性/改年龄都会作废年龄状态，`stepBlockers` 又强制要求重新套用。

**方案 A = 把这两份数据显式拆开**：分配值是玩家编辑的对象、也是校验和
年龄计算的基准；有效值是派生结果，只用于展示与下游计算。

---

# 第一批：后端 + SDK

> 必须先做完并验证通过，第二批（前端）依赖这里产出的 DTO 与 SDK 类型。

## B1-1 数据模型

`trpg-backend/app/models/room.py` 的 `Character` 新增一列：

```python
# 玩家在属性步骤分配出来的原始属性（年龄修正之前）。
# `attributes` 存的是**有效值**（年龄修正之后的最终属性，衍生值/技能基础值
# /职业技能点公式都基于它算）；而点数预算、掷点池总和、步进为 5 这三条
# 生成方法约束天然只对**分配值**成立——年龄修正必然把它们破坏掉
# （见 wizard-bugfix-round4.md #20）。两者拆开存，校验各取所需。
# 可空：本列之前建的角色卡没有这份数据，读取处一律回落到 `attributes`。
allocated_attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

新增一个 Alembic 迁移（`cd trpg-backend && .venv/bin/alembic revision --autogenerate -m "..."`，
生成后**务必人工检查**产物只包含这一列的 `add_column`，把 autogenerate
误带的无关改动删掉；跑 `.venv/bin/alembic upgrade head` 验证能升上去）。

⚠️ 迁移的 `down_revision` 要挂在当前 head 上，生成完用
`.venv/bin/alembic heads` 确认只有一个 head（这个仓库出过双 head 事故）。

## B1-2 规则层：校验基准与计算基准分离

`trpg-backend/app/core/coc7_rules.py`：

**(a) `_validate_attributes` 增加 `field_prefix: str = "attributes"` 参数**，
所有 `ValidationIssue.field` 从写死的 `f"attributes.{key}"` 改成
`f"{field_prefix}.{key}"`、`field="attributes"` 改成 `field=field_prefix`。
纯机械改动，不改任何判定逻辑。

**(b) `_compute` 增加 `allocated_attributes: dict[str, int] | None = None`
参数**，校验部分改成：

```python
if allocated_attributes is not None:
    # 分配值走完整的生成方法约束（预算/池值总和/步进为 5）。
    attribute_issues = _validate_attributes(
        ruleset, allocated_attributes, generation_method, attribute_pool_total,
        field_prefix="allocatedAttributes",
    )
    # 有效值（年龄修正后）只做宽松兜底：键齐全 + 落在 [1, 99]。
    # 借用 GENERATION_ROLL 这条已有分支——它恰好就是"只查结构和宽松区间、
    # 不查任何总和"，不需要新写一套校验。
    # 这一步不能省：`attributes` 是客户端可控输入，只校验分配值的话，
    # 客户端可以传一份合法分配 + 一份全 99 的有效值，把衍生值和职业
    # 技能点预算撑上天。
    attribute_issues += _validate_attributes(
        ruleset, attributes, GENERATION_ROLL, None, field_prefix="attributes",
    )
else:
    # 没传分配值：保持原行为，`attributes` 既当校验基准又当计算基准
    # （向后兼容——本列之前建的卡、以及不关心年龄修正的调用方）。
    attribute_issues = _validate_attributes(
        ruleset, attributes, generation_method, attribute_pool_total
    )
```

`_compute` 后续所有计算（`compute_derived_stats`、`evaluate_skill_base`、
`evaluate_skill_points_formula`、`interest_budget = INT*2`）**一律继续用
`attributes`（有效值）**，一处都不要改成 `allocated_attributes`。

**(c) `compute_preview` 和 `validate_character` 各自新增
`allocated_attributes: dict[str, int] | None = None` 参数并透传给
`_compute`。**

**(d)🔴 补一道"有效值不能凭空偏离分配值太多"的上限校验（Opus review 时
发现，用户已确认要堵）。**

问题：(b) 里给"有效值"用的宽松校验是"结构完整 + 落在 [1,99]"——但 99
本身就在 [1,99] 里，这道校验**完全拦不住**"分配值合法 + 有效值每项都
填 99"这种客户端伪造。而 `update_character`（PATCH）本身不做任何校验，
一次 PATCH 就能把"合法的 allocated_attributes + 全 99 的 attributes"
一起写进去，`complete_character` 时直接放行。

修法：在 `app/core/coc7_age.py` 新增一个从 `_AGE_TABLE`（含 `_UNDER_15`/
90+ 兜底档）**扫描**出来的常量，不要手写魔法数字：

```python
def max_total_adjustment_magnitude() -> int:
    """年龄修正理论上能让「分配值→有效值」总共偏离多少点——取全表最坏
    一档的 `EDU 改进检定总增量的上限（次数×10）+ |EDU 固定调整| + 身体
    减值 scd_loss + APP 减值 app_loss` 之和（同一档的 edu_checks 和
    edu_flat 互斥，不会同时非零，直接相加不会重复计）。这不是逐属性逐档
    精确重放（那需要另外跟踪"这一档到底能动哪几个属性"），而是一个保守
    的**总预算上限**：只要"有效值相对分配值的总偏离"不超过这个预算，
    客户端就不可能靠伪造有效值凭空多拿到规则允许范围之外的属性点，够
    堵住"全部属性怼到 99"这类粗暴伪造。"""
    rows = [m for _, _, m in _AGE_TABLE] + [_UNDER_15]
    return max(r.edu_checks * 10 + abs(r.edu_flat) + r.scd_loss + r.app_loss for r in rows)
```

`app/core/coc7_rules.py` 的 `_compute`（在 (b) 新增的"有效值宽松校验"
分支里）追加一条：

```python
from app.core.coc7_age import max_total_adjustment_magnitude

# ...在 allocated_attributes is not None 分支内，宽松校验之后：
point_buy_keys_for_bound = frozenset(a.key for a in ruleset.attributes if a.point_buy)
total_delta = sum(
    abs(attributes.get(k, 0) - allocated_attributes.get(k, 0))
    for k in point_buy_keys_for_bound
)
if total_delta > max_total_adjustment_magnitude():
    attribute_issues.append(
        ValidationIssue(
            code="EFFECTIVE_ATTRIBUTES_IMPLAUSIBLE",
            field="attributes",
            message=(
                f"有效属性相对分配值的总偏离 {total_delta} 超出年龄修正"
                f"理论上限 {max_total_adjustment_magnitude()}"
            ),
        )
    )
```

只在两边键都合法（前面的结构性检查已经过关）之后再算，避免缺键时
`abs(None-...)` 报错——按 (b) 现有分支顺序接在宽松校验后面即可。

`app/core/errors.py` 不需要改（这是 `ValidationIssue`，走的是
`CharacterInvalidError`/`compute_preview` 既有的校验报告通道，不是新的
`ErrorCode`）。

**补测试**：合法 `allocated_attributes`（比如八项都 60，池值 480，是不是
5 的倍数不重要，看你怎么构造）+ 全部有效值改成 99 → 必须出现
`EFFECTIVE_ATTRIBUTES_IMPLAUSIBLE`。再补一条对照：只改一两项、且总偏离
在 `max_total_adjustment_magnitude()` 以内（模拟真实年龄修正的量级）→
不应该报这条。

## B1-3 DTO

`trpg-backend/app/dto/character.py`：

- `CharacterPreviewRequest` 新增
  `allocated_attributes: dict[str, int] | None = None`，注释说明语义
  （分配值 vs 有效值，不传即退回"两者相同"的旧行为）。
- `CharacterUpdateBody` 新增同名同类型字段。
- `CharacterRead` 新增 `allocated_attributes: dict[str, int] | None = None`
  ——编辑已有角色卡时前端要靠它恢复"玩家原本分配了多少"。

## B1-4 Service

`trpg-backend/app/service/character.py`：

- `update_character`：`character.allocated_attributes = payload.allocated_attributes`
  （直接赋值，`None` 也照写——客户端明确不带就是没有这份数据）。
- `compute_character_preview`：把 `payload.allocated_attributes` 透传给
  `compute_preview`。
- `get_character`：`CharacterRead` 里带上 `allocated_attributes`。
- **`apply_age_adjustment` 改成基于分配值计算**（这是 #18 的修复）：
  ```python
  # 年龄修正必须永远基于「分配值」重算，而不是在上一次修正的结果上再修一次
  # ——后者会把扣减累加（实测 45 岁套两次：STR+CON+DEX 共扣 10，规则规定 5）。
  # 没有分配值的老角色卡回落到 attributes，保持原行为。
  source_attributes = (
      character.allocated_attributes
      if character.allocated_attributes is not None
      else character.attributes
  )
  ```
  用 `is not None` 而不是 `or`——`{}` 是 falsy 但不该被当成"没有值"
  （理论上不会真的存出一个空字典，但用显式判断更不容易踩坑）。
  然后把原本 `attributes_before = dict(character.attributes)` 那一段改成基于
  `source_attributes`；结果依旧写回 `character.attributes`，
  **不要动 `character.allocated_attributes`**。
  `AttributesNotSetError` 的判空条件相应改成判 `source_attributes`。
- **`complete_character`**：`validate_character(...)` 增加
  `allocated_attributes=character.allocated_attributes`（这是 #20 的修复）。

## B1-5 测试（本批最重要的产出）

`trpg-backend/tests/` 下补，每条都要能在改坏实现时报红：

1. **#20 回归**：掷点池角色，分配值总和 == 池值且每项都是 5 的倍数，
   有效值是"年龄修正后"的样子（含非 5 倍数、总和不等于池值），带上
   `allocated_attributes` 调 `compute_preview` → `validation == []`，
   且 `derived_stats`/两个技能点预算都是基于**有效值**算出来的正常数字。
   再补一条对照：**不**传 `allocated_attributes` 时同样的输入会报
   `INVALID_ATTRIBUTES`/`ATTRIBUTE_POOL_MISMATCH`（证明这条测试真的在测
   新加的分支，不是碰巧过）。
2. **#18 回归**：同一个 characterId 连续调两次 `apply-age-adjustment`
   （同一个年龄），断言第二次的 `attributesAfter` 里 STR/CON/DEX/APP 的
   扣减量跟第一次**相同**（不累加）。用 40–49 档的年龄（必有扣减），
   并注意 EDU 改进检定是随机的，断言只针对确定性的扣减项，不要断言 EDU。
3. **越权兜底**：传合法 `allocated_attributes` + 一份越界的
   `attributes`（比如某项 999）→ 必须报 `INVALID_ATTRIBUTES`
   （证明有效值那道宽松校验没被漏掉）。
4. `complete_character` 端到端：PATCH 带 `allocated_attributes` → 调
   apply-age-adjustment → complete 成功（此前会被 #20 拒掉）。
5. **(d) 总偏离上限**：合法 `allocated_attributes` + 全部有效值改成
   99 → 必须报 `EFFECTIVE_ATTRIBUTES_IMPLAUSIBLE`；对照一条总偏离在
   `max_total_adjustment_magnitude()` 以内的合法变动（模拟真实年龄修正
   量级）→ 不报这条。

## B1-6 SDK

`cd trpg-backend && .venv/bin/python scripts/export_schema.py`，然后
`cd trpg-sdk && npm run codegen`，把 `trpg-sdk/src/generated/dto.ts` 的
更新一并提交（这是既定流程，生成产物进 git）。检查
`trpg-sdk/src/resources/*.ts` 是否需要手写改动（预计不需要，payload 是
泛型透传，但要真的确认一遍）。

## 第一批验证要求

每项都要真跑、输出可见，不要重定向到 `/dev/null`、不要串成一条 `&&` 长链：

- `cd /Users/apple/Developer/work/AIDM_ALL/TRPG-master/trpg-backend && .venv/bin/python -m pytest`
- `.venv/bin/ruff check .`
- `.venv/bin/ruff format --check .`（容易漏，单独跑）
- `.venv/bin/ty check`
- `.venv/bin/alembic heads`（确认只有一个 head）
- `cd trpg-sdk && npm run typecheck && npm run lint && npm run build && npm run test`（分开跑，别串）
- `cd e2e && npm run test:e2e`

---

# 第二批：前端

> 等第一批验证通过后再做。

## B2-1 `wizard-state.ts`

- `attr` 的语义收窄为**分配值**（玩家分配的原始属性），
  **`APPLY_AGE_SUCCESS` 不再覆盖它**，`attrInputs` 同理也不再被覆盖。
- 新增 `attrAfterAge: Record<string, number> | null`：
  - `APPLY_AGE_SUCCESS` 时设为 `action.result.attributesAfter`；
  - `invalidateAge()` 里一并清成 `null`（改属性/改年龄/重掷都会走到这里）；
  - `createInitialWizardState()` 初始为 `null`；
  - `HYDRATE` 能被 patch 覆盖（跟其它顶层字段一样走展开赋值即可）。
- 在这个文件里给两个字段都写清楚注释，说明"分配值 vs 有效值"的分工，
  以及为什么不能合并（引用本文档）。

## B2-2 `wizard-selectors.ts` 新增一个派生函数

```ts
/** 角色的**有效属性**：年龄修正之后的最终值；还没套用过年龄修正时就是
 * 分配值本身。衍生值/技能基础值/职业技能点公式/角色卡展示一律用它；
 * 属性分配 UI 和三条生成方法约束（预算/池值/步进）用 `state.attr`。 */
export function effectiveAttr(state: WizardState): Record<string, number> {
  return state.attrAfterAge ?? state.attr
}
```

## B2-3 各消费方改用正确的那一份

| 位置 | 改动 |
|---|---|
| `AttributesStep.tsx` | **不改**——它本来就读 `state.attr`，语义现在正好是"分配值"。但要加一句提示：`state.attrAfterAge != null` 时在分配区上方显示"这里是你分配的原始点数；年龄修正后的最终值见「年龄」步骤和完成页"，避免玩家困惑两处数字不一样 |
| `useWizardPreview.ts` | 请求体 `attributes: effectiveAttr(state)`，新增 `allocatedAttributes: state.attr`。`attrsReady` 判断继续基于 `state.attr` |
| `AgeStep.tsx` | `syncCurrentStateToBackend` 要把**分配值**同步上去（见 B2-4）。其余不改 |
| `wizard-network.ts::syncCurrentStateToBackend` | `attr: state.attr`（分配值）**并新增** `allocatedAttributes: state.attr`。这样后端 `apply_age_adjustment` 永远基于干净的分配值算 |
| `useWizardSubmit.ts` | 最终 `previewCharacter` 用 `attributes: effectiveAttr(state)` + `allocatedAttributes: state.attr`；`saveCharacter` 的 `attr` 传 `effectiveAttr(state)`（角色卡存的是有效值）并新增 `allocatedAttributes: state.attr`；写进 `character-store` 的 `attr` 也用 `effectiveAttr(state)` |
| `FinishStep.tsx` | 摘要里的属性格子改用 `effectiveAttr(state)` |
| `useWizardHydration.ts` | 水合时：`attr` 取 `saved.allocatedAttributes ?? saved.attributes`；`attrAfterAge` 在 `saved.allocatedAttributes` 存在时取 `saved.attributes`、否则 `null`。它自己那次 `previewCharacter` 调用也要带上 `allocatedAttributes` |

`services/character/character-api.ts` 里 `saveCharacter` 的入参类型
（`BuiltCharacter` 或类似）需要相应增加 `allocatedAttributes` 字段并透传
给 SDK。

## B2-4 注意事项

- `stepBlockers('attrs')` 里的预算/池值判断继续基于 `state.attr`——**这正是
  #20 的关键**，不要顺手改成有效值。
- `AttributeAllocCard` 的"困难/极难"显示基于传入的 `value`（分配值）——
  保持不动即可（这一步玩家分配的就是这个数）。
- 不要动 `AgeAdjustmentReport`，它读 `state.ageResult` 的
  before/after，本来就是对的。

## 第二批验证要求

- `cd .../trpg-frontend && npx tsc -b`
- `cd .../trpg-frontend && npm run lint`
- `cd .../trpg-frontend && npm run build`
- `cd .../trpg-frontend && npm run test`（round2/round3 已有的两条用例必须
  继续绿；如果因为 `state` 新增字段需要调整测试夹具，改夹具即可，但**不要
  弱化任何断言**）
- 新补一条前端单测：`APPLY_AGE_SUCCESS` 之后 `state.attr` **保持不变**、
  `state.attrAfterAge` 等于 `attributesAfter`；随后触发一次
  `invalidateAge`（比如 `SET_ATTR_VALUE`）后 `attrAfterAge` 变回 `null`。
  这是方案 A 的核心不变量，必须有测试守着。做变异检验确认它真的报红。

---

## 通用要求（两批都适用）

- ⚠️ Bash 不要用裸 `cd xxx` 残留切换工作目录（这个 shell 会话是持久的、
  跟用户终端共享），每条命令用 `cd 绝对路径 && command`。
- 变异检验复原时用 Edit 精确改回或自己 `cp` 备份，**不要用
  `git checkout`**（这个仓库出过用它把未提交改动一起冲掉的事故）。
- 不要 `git commit`/`git push`，改完留在工作区。
- 不要加任何 Claude/AI 署名。
- 不需要浏览器点击验证。
