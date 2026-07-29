# 建卡向导 bugfix round5：技能点瀑布式记账（#22）+ 展示口径统一（#21）+ 案件简报常驻（#1）

> 对应 `docs/keeper-design/exec/12-真人实测问题清单-追书人.md` 的 #1 / #21 / #22。
> 三项彼此独立（不同文件、不同子系统），按下面两个批次并行执行，互不阻塞。

## 背景

用户真人实测拍板的三个方向（已经过 Opus 分析+用户确认，不需要再讨论设计）：

- **#22**：给★职业技能加点时，兴趣点数条不动。COC7 规则本身没错（兴趣点确实
  能花在任何技能，包括职业技能），但当前记账是"按技能身份二选一"——职业技能
  的分配点数 100% 计入 `occupation_spent`，哪怕职业预算已经花完也不会溢出到
  `interest_spent`。用户选择：改成**瀑布式记账**。
- **#21**：职业技能步骤顶部 PoolBar 只显示单池"还剩0点"，底部确认按钮显示
  两池合计"还剩76点"，同一句"还剩X点"文案两种口径。用户选择：顶部也改成
  显示合计，全程统一口径（配合 #22 一起做，瀑布式记账落地后这个合计更有
  意义）。
- **#1**：建完卡进游戏房间后，`player_intro`（案件简报）再也无处可查（只在
  建卡前的 `StoryPage` 出现一次）。用户选择：笔记 tab 顶部加常驻只读区。

---

## 批次 A（后端 + 前端 #21/#22，同一个 agent 顺序做）

### A1 后端：`trpg-backend/app/core/coc7_rules.py::_compute()` 改瀑布式记账

现状（约 526-598 行）：

```python
occupation_spent = 0
interest_spent = 0
...
for spec in ruleset.skills:
    ...
    effective_allocated = max(allocated, 0)
    if is_credit:
        if occupation is not None:
            occupation_spent += occupation.credit_min
            interest_spent += max(0, current - occupation.credit_min)
        else:
            interest_spent += max(0, current)
    elif spec.id in occupation_skill_ids or spec.id in slot_occupied_ids:
        occupation_spent += effective_allocated
    else:
        interest_spent += effective_allocated
```

**目标**：职业技能（含占槽技能，不含信用评级——信用评级的 floor/excess 拆分
是 Chaosium 官方裁定，独立生效、不要动）合计的分配点数，先扣职业预算，扣完
了溢出部分转记进兴趣点。**这是纯粹的重新分桶（re-bucketing），不改变
`total_spent = occupation_spent + interest_spent` 的总和，不改变任何一条
`ValidationIssue` 判定的最终结果**——这一点必须在实现里用测试证明，不是口头
保证。

**实现方式**：把当前循环拆成两遍。

1. 第一遍（现有的 for 循环基本不变，只改动 credit 之外分支的记账目标）：
   - 收集 `raw_occupation_skill_total`：所有 `spec.id in occupation_skill_ids
     or spec.id in slot_occupied_ids`（非信用评级）的 `effective_allocated`
     之和。
   - 信用评级仍按原逻辑：`credit_occupation_portion = occupation.credit_min`
     （或职业为空时 0），`credit_interest_portion = max(0, current -
     credit_min)`（或 `max(0, current)`）。
   - 非职业技能仍然 100% 直接计入 `interest_spent`（这部分逻辑不变）。
   - `skill_view` 列表的构建、每个技能的 `SKILL_BELOW_BASE`/`SKILL_ABOVE_CAP`
     校验、`allocations`/`slot_occupied_ids` 的计算**全部不变**——瀑布式只
     改总和怎么在两个桶之间分配，不改任何单项技能的展示值（`current`/
     `allocated`/`base`/`cap` 照旧）。
2. 循环结束后做一次性瀑布分配：
   ```python
   occ_budget_remaining_for_skills = max(0, occupation_budget - credit_occupation_portion)
   occ_spent_for_skills = min(raw_occupation_skill_total, occ_budget_remaining_for_skills)
   overflow_to_interest = raw_occupation_skill_total - occ_spent_for_skills

   occupation_spent = credit_occupation_portion + occ_spent_for_skills
   interest_spent = credit_interest_portion + overflow_to_interest + raw_interest_skill_total
   ```

**不变量（写进测试，用真实数字断言，不是靠读代码相信）**：
- 任意输入下，`occupation_spent + interest_spent` 与旧实现算出的总和完全相等
  （最简单的证明方法：写一个测试用旧逻辑和新逻辑各跑一遍同一份 `skills`
  输入，断言两者的 `occupation_spent + interest_spent` 相等，即使各自拆分
  不同）。
- `occupation_spent <= occupation_budget` 恒成立（瀑布分配保证职业桶不会
  超"支"，多余的必然转到兴趣桶）。
- 现有的 `SKILL_POINTS_EXCEEDED`（查总预算）、`INTEREST_POINTS_EXCEEDED`
  （查兴趣桶）两条校验的**触发条件**不变——即：任何在旧逻辑下合法的卡，
  新逻辑下依然合法；任何在旧逻辑下因为总预算超支而不合法的卡，新逻辑下
  依然不合法。这个不变量已经过数学推导（职业桶恒 ≤ 职业预算 ⟹ 当
  `total_spent ≤ total_budget` 时 `interest_spent ≤ interest_budget` 自动
  成立），**用具体数字的回归测试验证一遍**，不需要重新推导。
- 新增场景：一张卡的职业技能分配总量**超过**职业预算（这在瀑布式之前也
  合法，只是记账全堆在职业桶），瀑布式下应该能看到 `interest_spent` 里
  出现来自职业技能溢出的部分，且 `occupation_spent == occupation_budget`
  正好打满。

**测试**：在 `trpg-backend/tests/test_coc7_rules.py` 补：
1. 一个职业技能分配远超职业预算的场景 → 断言 `occupation_spent ==
   occupation_budget`、`interest_spent` 比"不溢出"场景高出恰好等于溢出量。
2. 一个职业技能分配没有花完职业预算的场景（现状里最常见的场景）→ 断言
   `occupation_spent`/`interest_spent` 与旧逻辑结果完全一致（不应该有任何
   行为变化，这是回归保护）。
3. 总预算恰好用满、恰好超一点两种边界场景 → 断言 `SKILL_POINTS_EXCEEDED`
   触发与否跟瀑布式记账前的逻辑推导一致。
4. 信用评级仍然独立按官方裁定分账，不受这次改动影响（复用/改写现有相关
   测试，确认没被误伤）。

不需要改 `compute_preview`/`validate_character` 的签名或 DTO——这是纯内部
记账重排，返回的 `SkillPointsBudget`（budget/spent/remaining）结构不变，
只是 `spent`/`remaining` 的数值分布变了。**不需要跑 SDK codegen**（DTO 形状
没变）。

### A2 前端：`PoolBar`（职业技能步骤顶部）改显示两池合计

先找到职业技能步骤渲染 PoolBar 的地方（`trpg-frontend/src/routes/games/trpg/
character-wizard/steps/` 下职业技能相关 step 组件，搜 `PoolBar`、
`还剩`、`occupation_spent`/`occBudget` 之类关键字定位）。

- 顶部 PoolBar 目前只算当前单池 `remaining = occBudget - occSpent`；改成用
  `wizard-selectors.ts::totalPointsRemaining()`（已存在，就是底部按钮在用
  的那个函数）算合计，文案统一成"职业+兴趣技能合计还剩 X 点"（或者更简洁
  的说法，只要顶部/底部完全一致即可，具体措辞你可以微调，但两处必须一字
  不差用同一个来源/同一句模板）。
- 兴趣技能步骤如果也有类似的单池 PoolBar，同步处理（保持两个步骤口径
  一致，不要只改职业技能这一个页面）。
- **不要改变任何校验逻辑**——这是纯展示层改动，`stepBlockers`/软性确认
  弹窗的判断逻辑不变。

**测试**：如果 `PoolBar` 或相关 selector 有现成的 vitest 文件就补充用例；
没有的话不需要新建测试文件，靠 tsc/eslint/手工读 diff 确认接线正确即可
（这是纯渲染层改动，不涉及状态机变化）。

---

## 批次 B（前端 only，独立 agent）：案件简报常驻展示（#1）

**目标**：`RoomPage.tsx` 的"笔记"tab（`BottomPanel open={openPanel ===
'notes'}`，约 1242-1264 行）顶部，加一块**常驻只读**的"案件简报"区域，
展示 `player_intro`，下面才是玩家自己的自由记事文本框（`notes`
textarea，逻辑完全不变）。

**数据来源**：`RoomPage.tsx` 已经在用 `sdk.modules.getDetail(moduleId)`
拿过 `detail.playerIntro`（约 482-486 行，目前只在替换旧占位叙事文本时
临时调用一次）。这次需要把这个数据**提升成组件级 state**，在房间加载时
就 fetch 一次并缓存（不要每次打开笔记面板都重新请求），供笔记面板渲染
使用。

**实现要点**：
- 新增一个 state，如 `const [playerIntro, setPlayerIntro] = useState<string
  | null>(null)`，在已有的 `moduleId` 可用的 effect 里（或新开一个小
  effect）调用 `sdk.modules.getDetail(moduleId)` 填充；只需要请求一次，
  不需要跟着 WS 事件刷新（`player_intro` 建卡前就已经固定不变）。
- 笔记面板内，`playerIntro` 非空时，在现有"添加线索标签"/"保存"按钮组
  上方渲染一块视觉上明显区别于自由记事区的只读卡片（比如带个"📋 案件
  简报"小标题 + 浅色背景框），内容就是 `playerIntro` 原文，不做任何二次
  加工/裁剪。
- `playerIntro` 为空/请求失败时，这块区域不渲染（不占位、不报错），笔记
  面板退化回现状（只有自由记事框）——不能因为这个新功能让笔记面板在没
  拿到数据时出错或空白卡住。
- 不要动 `notes`/`notesKey`/`lastSaved` 这套已有的自由记事逻辑，只是在它
  上方插入这块新区域。

**测试**：这是纯 UI 展示改动，跑 tsc/eslint/build 确认无误即可；如果想
补一个 vitest 测试渲染出现/不出现该区域也可以，不强制。

---

## 通用要求（两个批次都适用）

- 改完在 `docs/keeper-design/exec/12-真人实测问题清单-追书人.md` 里把 #1/
  #21/#22 的状态更新为"已修复（已提交）"，写清楚具体改法（跟之前 round1-4
  的记录风格一致）。
- commit message 不要加任何 AI 署名（不要 `Co-Authored-By: Claude`，不要
  `Generated with Claude Code` 这类）。
- 完成后把改动了哪些文件、跑了哪些验证命令（pytest / ruff / ty / tsc /
  eslint / build，输出要真实可见，不要重定向到 /dev/null 也不要串成一条
  长 `&&` 链导致某一步失败被掩盖）如实报告，不要只说"已完成"。
