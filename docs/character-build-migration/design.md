# 建卡系统迁移设计:coc-char-gen → trpg-backend/trpg-sdk/trpg-frontend

> 2026-07-28。来源:`/Users/apple/Developer/personal/coc-char-gen`(用户此前让 Grok 生成的独立
> 建卡前端,纯 vanilla JS,无构建工具)。目标:把它设计得好的部分迁移进当前项目,规则计算/
> 数据归后端,前端只渲染(项目既定"路线乙"原则),角色导出部分重新设计。

## 一、现状核对(已读代码确认)

### coc-char-gen 有、当前项目没有的能力

| 能力 | coc-char-gen 位置 | 当前项目状态 |
|---|---|---|
| 年龄调整(EDU改进检定/STR-SIZ或STR-CON-DEX减值/APP减值/MOV年龄惩罚/青年幸运双掷) | `js/plugins/age.js` | **完全没有**——`trpg-backend/app/dto/game.py:125-127` 的 `AgeRangeSpec` 注释明确写着"本期只做区间约束,不做年龄修正...那是一整套生成期规则,要单独做"。这是一个项目自己早就标注、一直搁置的缺口。 |
| 属性生成法:掷点池(5×3d6×5+3×(2d6+6)×5求和成池,玩家手动分配到八维) | `js/core/dice.js::rollAttributePointPool` | 只有两种:`pointbuy`(固定480预算购买)、`roll`(8项属性直接各自掷定,不能分配)。掷点池是介于两者之间的第三种规则书认可的变体。 |
| 结构化背景故事(信念/重要之人/意义之地/珍视之物/特质/外伤/恐惧症,共8个引导字段) | `js/core/character.js::createEmptyCharacter().background` | 只有 `background`(自由文本)+`notes`(自由文本)两个扁平字段。 |
| 伤害加值/体格完整表(到 +4D6/体格5,超出按规律延伸) | `js/core/engine.js::damageBonusAndBuild` | **`trpg-backend/app/core/coc7_rules.py:90-103` 的 `_damage_bonus_and_build` 是错的**——sum>204 时硬编码返回 `"+1D8"`,但 COC7 官方表里根本没有 "+1D8" 这一档,204 之后应该是 `+2D6`(build 3)、`+3D6`(build 4)……一直延伸。这是个真 bug,不是"缺功能"。 |
| MOV 扣年龄惩罚 | `js/core/engine.js::movementRate` | `compute_derived_stats` 算 MOV 时完全不考虑年龄,40 岁起每档 MOV 应该 -1/-2/-3/-4/-5。 |
| 导出格式(骰娘 `.st` 完整/精简、文本卡、JSON) | `js/plugins/exporters.js` | **完全没有**——用户明确说这部分"不够优秀或不适合目前的后端和SDK生态",要我重新设计。 |

### coc-char-gen 有、但不应该原样搬过来的部分

| 部分 | 为什么不搬 |
|---|---|
| `js/core/engine.js` 全量计算逻辑 | 当前 `coc7_rules.py` 更完整(229 职业真实数据、自选槽全局最优分配、信用评级区间、结构化校验),这部分除了上面表格里列的具体缺口外,不需要整体替换。 |
| `js/core/dice.js` 里 `Math.random()` 直接产值 | 项目原型取舍早就否决了客户端掷骰(`docs/keeper-design` 及 `#77` 决策)——所有随机数必须服务端权威生成。dice.js 里的**骰子公式**(3d6×5、2d6+6×5、EDU改进 d100>EDU 则+1d10)要保留,**执行位置**必须搬到后端。 |
| `js/plugins/slots.js` 的显式选槽 UI 模型(玩家为每个槽位显式指定"选哪个具体技能") | 当前后端 `_assign_choice_slots`(`coc7_rules.py:194`)是"玩家只管往任意技能里塞点数,后端做全局最优分配"的隐式模型——这是 229 职业目录那一整轮工作硬啃出来的成果(issue #114/#119),经过真实数据校验、变异测试。**不替换**,保留隐式分配作为权威裁决。coc-char-gen 的显式选槽 UI 仅供前端在"这个职业本职技能大概是哪些"的展示/引导上参考,不改变提交给后端的数据形状。 |
| `js/core/registry.js` 插件注册表架构 | vanilla JS 特有的可扩展性模式,React + 类型化后端 DTO 已经是更好的替代方案,不需要。 |

## 二、迁移方案

### 后端(`trpg-backend`)

1. **年龄调整**——新文件或扩展 `coc7_rules.py`:
   - 移植 `AGE_TABLE`(7 档,15-89 岁)+ `get_age_modifiers(age)`。
   - `distribute_scd_loss(attributes, loss, only_str_siz) -> dict`——按规则把体质/力量/敏捷(或青年档的力量/体型)减值轮转分摊,最低到 1。
   - `apply_app_loss(attributes, loss) -> dict`。
   - `roll_edu_improvement(edu) -> (success, roll, gain, new_edu)`——服务端权威 `d100`,复用 `service/character.py::_roll` 的模式;`eduChecks` 次数按年龄档(20-39 一次,40-49 两次……)循环调用。
   - 青年档(15-19)幸运双掷取高——复用现有幸运掷骰逻辑,双掷一次。
   - **修复** `_damage_bonus_and_build`:按 coc-char-gen 完整表重写(204 之后延伸到 +2D6/+3D6/+4D6/……,不是硬编码 +1D8)。
   - `compute_derived_stats` 加 `age: int | None = None` 参数,MOV 扣 `movPenalty`。
   - 新端点 `POST /rooms/{roomId}/characters/{characterId}/apply-age-adjustment`,body `{age: int}`:读当前 `character.attributes`(必须已存在,否则 409),套用年龄表算出 EDU 改进/减值明细,写回 `attributes`,写 `character.age`,返回调整明细(供前端展示"发生了什么")。

2. **掷点池生成法**:
   - `coc7_rules.py` 新增常量 `GENERATION_ROLL_POOL = "roll_pool"`。
   - `Character` 模型(`app/models/room.py`)新增可空列 `attribute_pool_total: int | None`(Alembic 迁移)——掷池子时把权威总值记下来,后续校验"玩家分配总和是否等于池子总值"要有个真实依据,不能只信任前端报的数。
   - 新端点 `POST /rooms/{roomId}/characters/{characterId}/roll-attribute-pool`:服务端掷 5×3d6×5+3×(2d6+6)×5,把明细和 total 返回,写 `character.generation_method = "roll_pool"` + `character.attribute_pool_total = total`(不写 `attributes`,分配是后续 PATCH 完成的)。
   - `_validate_attributes` 新增 `roll_pool` 分支:八维总和必须等于 `attribute_pool_total`,单项 [15,90] 且是 5 的倍数(复用 `pointbuy` 分支已有的区间/step 校验代码路径,只是预算来源换成 `attribute_pool_total` 而不是 `ruleset.attribute_point_buy.budget`)。

3. **结构化背景故事**:
   - `Character` 模型新增可空 JSON 列 `background_detail: dict | None`(跟 `attributes`/`skills` 同样的 JSON 列模式),存 8 个字段:`personal_description`/`ideology`/`significant_people`/`meaningful_locations`/`treasured_possessions`/`traits`/`injuries`/`phobias`。**不删除**现有 `background`/`notes` 扁平字段(向后兼容、风险低)。
   - `CharacterUpdateBody`/`CharacterRead` 加 `background_detail: dict[str, str] | None`。

4. **DTO/迁移**:上述改动涉及 `dto/character.py`、`dto/game.py`(如需要暴露年龄表)、`models/room.py`(2 个新列)、一份新 Alembic revision、`service/character.py` 新函数、`controller/` 新路由、`ErrorCode` 如需新错误码(如 `ATTRIBUTES_NOT_ROLLED_YET`)。
   - `scripts/generate_api_docs.py` 跑一遍同步 `docs/API.md`(如该分支需要)。

### SDK(`trpg-sdk`)

1. 后端改完后跑 `npm run codegen` 同步生成类型。
2. `CharactersResource` 加方法:`rollAttributePool`、`applyAgeAdjustment`。
3. **新增导出格式化模块** `src/format/character-export.ts`(纯函数,零网络请求,零运行时依赖,符合 SDK 一贯定位):
   - `formatDicebotFull(character, compute)` / `formatDicebotShort(...)`——移植 `exporters.js` 的骰娘 `.st` 全量/精简格式化逻辑,技能别名表(计算机使用→计算机/电脑等)原样搬。
   - `formatTextCard(character, compute)`——人类可读文本卡。
   - `formatJson(character, compute)`——`JSON.stringify` 打包。
   - 入参改用后端已经返回的 `CharacterRead` + `CharacterComputeResult`(不重新计算任何规则数值,纯格式化已经算好的权威数据)。
4. 补单元测试(SDK 已有 e2e/单测基础设施,参照现有测试写法)。

### 前端(`trpg-frontend`)

1. `data/character-model.ts` 扩展类型:`backgroundDetail`、年龄调整明细、`generationMethod` 增加 `'roll_pool'` 选项、掷点池分配的向导状态。
2. `CharacterPage.tsx` 向导扩展(在现有 4 步基础上插入,不推翻重写):
   - 属性步骤内(或作为其后的子步骤)加"年龄调整"环节:展示年龄表、调用 `apply-age-adjustment`、展示 EDU 改进检定过程(掷骰明细,类似现有 roll-attributes 的展示方式)与最终减值结果。
   - 背景故事扩展成结构化 8 字段表单(可在现有"完成"步或单独插入一步,视现有 UI 空间决定,由执行时判断)。
   - "完成"步之后/内加"导出"面板:调用 SDK 格式化函数,展示骰娘 `.st`(完整/精简 tab)、文本卡、JSON,带复制按钮。
   - 属性生成方式如果要暴露"掷点池"选项,在信息步或属性步加一个方法选择(直接掷 / 点数购买 / 掷点池),对应调用不同后端端点。
3. `character-store.ts` 的 `CompletedCharacter` 加 `backgroundDetail` 字段。
4. `services/character/character-api.ts` 加对应的 SDK 调用封装。

## 三、执行阶段划分(串行,后一阶段依赖前一阶段的真实产出)

1. **Phase 1(后端)**:上述"后端"全部内容 + pytest 覆盖(年龄表边界、EDU改进掷骰、掷点池校验、DB/Build 修复的回归用例)+ ruff/ty 过。
2. **Phase 2(SDK)**:codegen 同步 + 新增 resource 方法 + 导出格式化模块 + 单测。
3. **Phase 3(前端)**:向导扩展 + 导出面板 + tsc/eslint/build 过。
4. **Phase 4(验证)**:pytest 全量、e2e 全量、前端 tsc/eslint/build,浏览器真机走一遍新增的年龄调整/导出流程(Browser 面板)。

## 四、不在本次范围内(如需要,后续单独做)

- 显式选槽 UI(保留隐式全局最优分配)。
- "我的常用角色卡库"复用逻辑(项目本来就标注"本期不实现")。
- 从 coc-char-gen 迁移 CSS/视觉设计细节——按当前 trpg-frontend 已有的 Tailwind 风格重新实现交互,不直接照搬 vanilla CSS。

## 五、版权/来源说明

coc-char-gen 是用户个人项目(Grok 生成 + 用户自己维护),职业数据来自 `COC7空白卡CY23Final.xlsx`(CY 作者丛雨,规则版权 Chaosium)——这条链路跟当前项目 `coc7_content.py` 的数据来源完全一致,不引入新的版权问题。本次只搬**逻辑**(年龄表数值、骰子公式、导出格式字符串模板),不搬 `data/occupations.json`(当前项目自己的 229 职业数据已经更完整、经过校准)。
