"""规则原语：模组里 NPC 实体的寻址（把模型写的名字解析成白名单 id）。

**不属于任何一个能力**——health 要用它给 NPC 记血，world_state 要用它给
`state_updates.subject` 找主体。留在 health 里就会变成"world_state import
health"，那正是架构测试禁止的跨能力依赖（`exec/27`：能力之间不许互相 import，
共用的东西下沉）。

🔴 全部**精确**匹配，不做包含/模糊——`exec/17` 记过一次，模糊匹配是同义词
打地鼠的开始，正解是白名单。

（`dice` 与技能 id 白名单在阶段 5 目录终态时一并搬进这个包。）
"""

from __future__ import annotations

import re

from app.core.keeper.contract.module_loader import ScenarioModule


def resolve_npc_id(module: ScenarioModule, label: str) -> str | None:
    """把裁决器写的名字解析成模组里的 npc id。解析不出返回 None。

    匹配顺序：npc id → npc name → 形态 id → 形态 name。全部**精确**匹配
    （去空白、忽略大小写）。
    """
    key = (label or "").strip().casefold()
    if not key:
        return None
    for npc in module.npcs:
        if npc.id.casefold() == key or npc.name.casefold() == key:
            return npc.id
    for npc in module.npcs:
        for form in npc.forms:
            if form.id.casefold() == key or (form.name or "").casefold() == key:
                return npc.id
    return None


def resolve_npc_ref(module: ScenarioModule, label: str) -> str | None:
    """解析到**最具体**的那一层：命中形态就返回**形态 id**，不上溯到本体。

    ## 🔴 为什么要跟 `resolve_npc_id` 分成两个

    2026-08-14 实测：裁决写 `npcs_on_stage: ["alan-devereux"]`，落库却成了
    `["main-npcs"]`——因为 `alan-devereux` 是 `main-npcs` 的一个**形态**，
    而那个函数按设计上溯到本体。于是「此刻台上是谁」永远只能答"主要NPC"，
    `cast` 这片能力想解决的问题在这个模组上原样存在。

    根子在导入数据：`forms` 的语义是"同一个体的不同形态"（成体/幼体、
    被揭穿时），而导入管线把**四个不同的人**塞进了一个叫「主要NPC」的容器。

    🔴 **但"该不该拆"判不了。** 三份真实样本：`main-npcs`（本体无数据卡、
    4 个形态是四个人）、`mi-go-encounter`（本体**有**数据卡、4 个形态是四只
    不同的米-戈）、`butler-secret`（本体有数据卡、1 个形态叫「被揭穿时」）
    ——前两个是容器、第三个是真·多形态，而**结构上分不开**。真正的区别是
    形态名是"人名"还是"状态描述"，那是语义判断。同 `exec/29` 里「该不该成为
    节点」那条：已判定做不到，别再找第八个候选。

    所以不去拆数据，改成**按用途选粒度**：

    - **血是个体的** → `resolve_npc_id`（上溯本体）。真·多形态的两种样子共用
      一条血，那是对的。
    - **台上是谁、谁掷这一下是形态的** → 这个函数。模型说 `alan-devereux`
      时它指的就是那一位，不该被替换成容器名。
    """
    key = (label or "").strip().casefold()
    if not key:
        return None
    for npc in module.npcs:
        if npc.id.casefold() == key or npc.name.casefold() == key:
            return npc.id
    for npc in module.npcs:
        for form in npc.forms:
            if form.id.casefold() == key or (form.name or "").casefold() == key:
                return form.id
    return None


def npc_display_name(module: ScenarioModule, npc_id: str) -> str:
    """id → 展示名。**认形态**：`resolve_npc_ref` 会交出形态 id，两边要配套。"""
    for npc in module.npcs:
        if npc.id == npc_id:
            return npc.name
        for form in npc.forms:
            if form.id == npc_id:
                return form.name or form.id
    return npc_id


#: 数据卡上"这一项是个属性点"的键。COC7 里属性点要 ×5 才是百分位目标值，
#: 而攻击项（"爪击 70% 1D6"）本身就写成了百分数。两类换算不同，只能逐个列出
#: ——**这是个「逐个列出的地方」**，加一条属性轴要回来加一行。
_ATTRIBUTE_KEYS = ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUCK", "POT")

#: 数据卡上**永远不是可掷目标值**的键：别的量纲（伤害加成、护甲、体格）或
#: 别的机制（目击理智损失）。拿它们掷 d100 没有意义。
#:
#: 🔴 这是个**逐个列出的地方**，加一种数据轴要回来加一行。但列出来比不列好：
#: 不列的话它们会出现在 `npc_ability_names` 给模型的候选里，等于请它挑错。
_NON_ABILITY_KEYS = frozenset(
    {
        "damage_bonus", "armor", "hp", "mp", "san", "move", "mov", "db", "build",
        "san_loss_on_sight", "理智损失", "伤害", "护甲", "体格", "备注",
    }
)  # fmt: skip

#: 百分数**出现在任意位置**都算。
#:
#: 🔴 第一版只认开头，那是照着**一个**样本调出来的。六份真实模组量下来它只
#: 覆盖 43%——真实写法里百分数常在中间（`斗殴 50%`、`格斗 40%(困难20/极难8)`）。
#: 更糟的是我当时读的是**库里那份被归一过的**数据，磁盘上的原始 structured
#: 里同一条写的是没有 % 的 `70 1D6+1D6`。**对着一个样本调正则，还当它是通例。**
_PERCENT_ANYWHERE = re.compile(r"(\d{1,3})\s*%")

#: 没有 % 时的第二档：开头的裸数字，**且后面跟着空白或括号**。
#:   `70 1D6+1D6` → 70 ✓        `40（伤害值见上文）` → 40 ✓
#:   `1/1d3` → 不取（后面是 `/`）  `4D6` → 不取（后面是 `D`）
#:   `13预设调查员CALL OF…` → 不取（后面是汉字）——那是 PDF 抽取的垃圾行
#: 后置断言就是用来挡最后那类的：数字紧贴着别的字符时它不是一个独立的值。
#:
#: 两档合起来在六份模组的 31 条可掷候选里取到 28 条（90%），漏的 3 条
#: （垃圾行 / `无` / 没有百分数的战技）**本来就该拒**。
_LEADING_BARE = re.compile(r"^\s*(\d{1,3})(?=[\s（(])")


def _stats_of(module: ScenarioModule, npc_id: str) -> dict | None:
    """这个 id 的数据卡。**形态优先**：形态有自己的数据卡时用它自己的。"""
    for npc in module.npcs:
        if npc.id == npc_id:
            return npc.stats or {}
        for form in npc.forms:
            if form.id == npc_id:
                return form.stats or {}
    return None


def npc_ability_names(module: ScenarioModule, npc_id: str) -> list[str]:
    """这个 NPC **可以拿来掷**的那些项。裁决器只能从这里面挑，挑不中就是编造。

    过滤掉别的量纲（护甲、伤害加成）与别的机制（目击理智损失）——把它们留在
    候选里等于请模型挑错。
    """
    stats = _stats_of(module, npc_id) or {}
    return [k for k in stats if k not in _ATTRIBUTE_KEYS and k.lower() not in _NON_ABILITY_KEYS] + [
        k for k in stats if k in _ATTRIBUTE_KEYS
    ]


def npc_check_target(module: ScenarioModule, npc_id: str, ability: str) -> int | None:
    """NPC 拿哪个数掷。取不到就返回 None——**由调用方拒绝这次检定**。

    🔴 **取不到就不掷，不猜**（用户 2026-08-15 拍板的不对称）：名册里有数据卡
    的 NPC 用它自己的数值真掷；即兴造出来的 NPC 没有数据卡，那一格就该空着，
    由叙事直接裁定。让裁决器现编一个目标值等于把难度交给模型自己定，正是
    「能确定化的是判断的**输入**，不是判断本身」要避免的。

    两种取值方式，按数据卡上那一项自己的形状分：

    - **属性点**（`STR 15`）→ ×5 换成百分位（COC7）；
    - **攻击项**（`爪击 70% 1D6+1D6 …`）→ 取开头那个百分数。

    键名要求**一字不差**：`ability` 是从数据卡里挑的，而数据卡整段就摆在裁决
    器眼前（`render_npc`）。做模糊匹配就又回到同义词打地鼠（exec/17）。
    """
    stats = _stats_of(module, npc_id)
    if stats is None:
        return None
    raw = stats.get(ability)
    if raw is None:
        return None
    if ability in _ATTRIBUTE_KEYS:
        try:
            return max(1, min(100, int(str(raw).strip()) * 5))
        except ValueError:
            return None
    if ability.lower() in _NON_ABILITY_KEYS:
        return None
    text = str(raw)
    matched = _PERCENT_ANYWHERE.search(text) or _LEADING_BARE.match(text)
    if matched is None:
        return None
    value = int(matched.group(1))
    # 🔴 越界就**拒绝**，不夹紧：夹紧等于把一个抽取错误变成看似合理的规则结论
    # （同对抗检定目标值那道校验的理由）。
    return value if 1 <= value <= 100 else None
