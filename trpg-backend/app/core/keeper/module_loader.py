"""结构化剧本数据的加载（keeper agent 实验）。

剧本文件是从模组 PDF 人工结构化出来的 JSON（如 `模组资料/追书人.structured.json`），
**gitignore、不进公开仓库**——第三方模组的版权不属于本项目。这里只提交
「形状定义 + 加载器」；测试用 `tests/fixtures/` 下的原创迷你剧本。

形状刻意宽松（大量 Optional / 自由文本字段）。4b 起 schema 开始承担「实体图 /
形态 / 密级配对」的可寻址骨架位（见 docs/keeper-design/exec/07），但仍保持
向后兼容：旧 JSON 无新字段时 load_module 必须成功。完整分表（实体表 vs 状态机
独立）若仍堵死主持再做，不在本加载器一次做完。
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _LenientModel(BaseModel):
    # 剧本 JSON 里允许出现形状定义之外的字段（不同模组会有各自的自定义键），
    # 忽略而不是报错——加载器的职责是"读得出来"，不是"校验模组写法"。
    model_config = ConfigDict(extra="ignore")


class ModuleFact(_LenientModel):
    """一条**可被揭示的断言**——`view(subject)` 管辖的最小单位（exec/15）。

    为什么是模组顶层的表、而不是挂在 node/check 底下（实测结论，别改回去）：
    同一条 `on_success` 在多个检定下重复出现（神秘渡轮 71→57、死者的顿足舞
    34→27），这是 COC 的**多路径线索**设计——同一条信息可以用不同技能、在不同
    地点拿到。挂在 node/check 下面会让同一条线索变成多条 id，于是「谁知道了
    什么」把一件事记成两笔，线索账本从第一天就是错的。

    判据（exec/14 决策②）：**秘密必须有 id；没有 id 的东西就只能是颜色。**
    所以只有"可被揭示的断言"进这张表，场景描写/氛围/即兴内容一律不进——
    全量实体化会把世界变成查表，压死守秘人的即兴空间。
    """

    id: str
    text: str
    # clue（线索）| npc_knowledge（NPC 在虚构内知道的）| truth（真相层）
    kind: str = "clue"
    # diegetic：虚构内，可被挣得翻面
    # meta：元层（结局/议程/主持指导），对**任何**虚构内主体永不可见
    tier: str = "diegetic"
    # 迁移出处（如 "node:<id>.checks[2].on_success"），供审计与重放
    origin: str | None = None


class ModuleCheck(_LenientModel):
    """一次检定点：技能、难度与成败后果。

    ## 两个技能字段的分工（exec/17 (A)）

    - `skill_ids`：**机器可读**，组装期归一产出的规则表 id 列表。护栏比对、
      账本 reveals 匹配只看它——两侧都是 id，运行时一个字符串匹配都不剩。
      多个 id = **任一命中即可**（"话术/魅惑/信用"这类多选检定点，以及
      "STR+DEX"这类组合属性）。
    - `skill`：**展示名**，渲染进 KP 剧本正文给模型读。组装期归一成规则表
      规范中文名。

    为什么不是一个字段：id 给代码看、名字给人和模型看，两种受众对"同一个
    技能"的正确写法本来就不同。此前只有 `skill` 一个自由文本字段，于是
    43 条模组数据（含 11 条 COC6 遗留的「灵感/知识」）在运行时全靠字符串
    匹配去猜，猜不中就静默丢检定。

    `kind="san"` 的检定点是**理智检定**（模组里写作「理智」「理智(San)」）：
    它该走 `san_checks` 而不是技能检定，`skill_ids` 恒为空。
    """

    skill_ids: list[str] = []
    kind: Literal["skill", "san"] = "skill"
    skill: str = ""
    difficulty: str | None = None
    on_success: str | None = None
    on_failure: str | None = None
    on_fumble: str | None = None
    prerequisite: str | None = None
    # 成功时揭开哪些事实（引用 ScenarioModule.facts 的 id）
    reveals: list[str] = []


class ModuleBranch(_LenientModel):
    """节点内的条件分支（"若玩家杀死人影→…"）。`then` 允许嵌套一层。"""

    condition: str
    outcome: str
    then: list["ModuleBranch"] | None = None


class ModuleNode(_LenientModel):
    """一个调查节点/场景：KP 视角的完整信息 + 检定点 + 图边。

    图边语义（4b，口诀）：
    - exits：空间邻接（地图边）
    - contains：包含层级（地点↔物件/子区域 id）
    - leads_to：情节/因果推进边（调查解锁），不再兼作房间邻接表
    """

    id: str
    title: str
    kp_text: str
    # location | clue | item | event | … 自由短标签，内容层
    kind: str | None = None
    # 挣得/公开后可念给玩家的摘要；绝密仍在 kp_text
    public_text: str | None = None
    checks: list[ModuleCheck] = []
    branches: list[ModuleBranch] = []
    # 向后兼容：单子节点
    sub_node: "ModuleNode | None" = None
    # 可寻址多子节点（物件、子房间、日志等）
    sub_nodes: list["ModuleNode"] = []
    leads_to: list[str] = []
    exits: list[str] = []
    contains: list[str] = []
    # 进入/搜索该节点即可得（无需检定）的事实
    reveals: list[str] = []


class ModuleNpcForm(_LenientModel):
    """同一 NPC 的一个形态（成体/幼体、战斗形态等）。"""

    id: str
    name: str | None = None
    notes: str | None = None
    stats: dict | None = None


class ModuleNpc(_LenientModel):
    id: str
    name: str
    role: str | None = None
    kp_notes: str | None = None
    # 数据卡保持自由字典：不同怪物/NPC 的数据轴不一样，agent 直接读原文。
    stats: dict | None = None
    public_text: str | None = None
    forms: list[ModuleNpcForm] = []
    # 其它 npc id：公开形象行 ↔ 秘密行等「同一实体」链接
    same_as: list[str] = []
    # 该 NPC 在**虚构内**知道的事实。注意它常常**超过**玩家知道的（教徒知道
    # 教团、图书管理员知道书库布局）——NPC 缺的不是信息量，是元层（tier=meta）。
    knows: list[str] = []


class AgendaEvent(_LenientModel):
    """时间轴上的一个"该发生的事"——不依赖玩家行动，世界自己会动。"""

    id: str
    title: str | None = None
    # 自由文本，必填无默认。触发条件是"内容"（LLM 消费）不是"结构"（代码消费）：
    # 曾用 AgendaTrigger{type,at,seconds,note} 逼预处理 agent 做语义归类
    # （"第二个满月之夜"归哪个枚举？），枚举从一个模组倒推、归错就废。
    # 主持人手里有剧本全文 + 历史 + keeper_state 的游戏内时间，判断"本轮是否
    # 达成触发"本来就该它做；代码去算第几晚反而脆（实测措辞一变就误触发）。
    # 与 ModuleEnding.trigger（str | None）同构——内容层一律自由文本。
    trigger: str
    kp_text: str
    effects: list[str] = []
    once: bool = True


class ModuleOpening(_LenientModel):
    """开场脚本（提案③的 opening 阶段会消费，本期先让它存在并进 prompt）。"""

    scene: str | None = None
    script: str
    kp_notes: str | None = None


class ModuleEnding(_LenientModel):
    id: str
    title: str
    condition: str | None = None
    # 可判定的触发描述（提案③ ending_reached 消费）；condition 是原文叙述，
    # 两者共存——本期只存字段、只渲染，不做判定。
    trigger: str | None = None
    text: str


class ModuleMeta(_LenientModel):
    id: str
    title: str
    designed_players: str | None = None
    multi_player_note: str | None = None
    era: str | None = None
    tone: str | None = None


class KeeperTruth(_LenientModel):
    summary: str
    key_facts: list[str] = []


class VisibilityPair(_LenientModel):
    """同一事实的公开版 ↔ 真相版可遍历配对（静态骨架；翻面记账属路线第 5 步）。"""

    id: str
    public_ref: str
    secret_ref: str
    note: str | None = None


class ScenarioModule(_LenientModel):
    """一个可被 keeper agent 主持的完整模组。"""

    meta: ModuleMeta
    kp_truth: KeeperTruth
    player_intro: str
    # 时间骨架（提案①）：开场脚本 + 议程时间轴。全部可选/默认空，旧模组
    # 不含这些字段时照常 load_module 成功（向后兼容是硬要求）。
    opening: ModuleOpening | None = None
    agenda: list[AgendaEvent] = []
    nodes: list[ModuleNode] = []
    endings: list[ModuleEnding] = []
    npcs: list[ModuleNpc] = []
    kp_guidance: dict[str, str] = {}
    # 4b：密级配对表（空列表兼容旧模组）
    visibility_pairs: list[VisibilityPair] = []
    # P1：可寻址事实表（空列表兼容尚未迁移的模组；迁移在 P1.3）
    facts: list[ModuleFact] = []

    def node_by_id(self, node_id: str) -> ModuleNode | None:
        return _find_node(self.nodes, node_id)

    def fact_by_id(self, fact_id: str) -> ModuleFact | None:
        for fact in self.facts:
            if fact.id == fact_id:
                return fact
        return None

    def diegetic_fact_ids(self) -> set[str]:
        """虚构内可挣得的事实——`view(subject)` 的候选全集。

        meta 层不在其中：结局/议程/主持指导对任何虚构内主体永不可见，
        不存在"挣得"这回事。
        """
        return {f.id for f in self.facts if f.tier == "diegetic"}

    def npc_by_id(self, npc_id: str) -> ModuleNpc | None:
        for npc in self.npcs:
            if npc.id == npc_id:
                return npc
        return None

    def agenda_by_id(self, event_id: str) -> AgendaEvent | None:
        for event in self.agenda:
            if event.id == event_id:
                return event
        return None


def _find_node(nodes: list[ModuleNode], node_id: str) -> ModuleNode | None:
    for node in nodes:
        if node.id == node_id:
            return node
        if node.sub_node is not None:
            found = _find_node([node.sub_node], node_id)
            if found is not None:
                return found
        found = _find_node(node.sub_nodes, node_id)
        if found is not None:
            return found
    return None


def load_module(path: str | Path) -> ScenarioModule:
    """从结构化 JSON 文件加载模组。文件不存在/JSON 非法/形状不符都会抛异常——
    由调用方（build_narrator）决定回退策略，这里不吞。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return ScenarioModule.model_validate(raw)


def public_story_from_module(
    module: ScenarioModule,
) -> tuple[str | None, str | None, list[str]]:
    """抽取玩家可见前情，供建卡前的 story 界面展示。

    仅返回可直接给玩家看的文本；不含 kp_notes / kp_truth。

    🔴 真人实测 2026-07-28（神秘渡轮）复现：`pages` 曾经把 `player_intro`
    和 `opening.script` 拼在一起展示，但 `opening.script` 同时又被"开场
    仪式"复用为游戏正式开始后守秘人的第一句话——玩家会把同一段场景描写
    读两遍。`pages`（进而 `story_pages`）现在**只收 `player_intro`**：
    这个界面的定位是建卡前的简短钩子（类比 COC 模组书里"读给玩家听"的
    引子，不是完整开场戏），`opening.script` 专门留给开场仪式那一步，
    经 LLM 叙事层改写后才广播给玩家（见 `agent.py` 的 `is_opening_ceremony`
    分支）。`opening` 这个返回值仍然保留给调用方（`opening_script` 字段），
    不在这里删除——只是不再混进 `pages`。
    """
    intro = (module.player_intro or "").strip() or None
    opening: str | None = None
    if module.opening is not None:
        opening = (module.opening.script or "").strip() or None
    pages = [intro] if intro else []
    return intro, opening, pages


# ── 文本渲染（给 LLM 消费）────────────────────────────
# system prompt（剧本全文常驻）和 read_module 工具共用同一份渲染，保证
# agent 无论从哪条路看剧本，看到的都是同一个文本。


def render_overview(module: ScenarioModule) -> str:
    guidance = "\n".join(f"- {k}：{v}" for k, v in module.kp_guidance.items())
    facts = "\n".join(f"- {f}" for f in module.kp_truth.key_facts)
    return (
        f"《{module.meta.title}》（{module.meta.era or ''}）\n"
        f"基调：{module.meta.tone or ''}\n"
        f"多人适配：{module.meta.multi_player_note or ''}\n\n"
        f"【KP 真相（绝密）】{module.kp_truth.summary}\n关键事实：\n{facts}\n\n"
        f"【开场（可念给玩家）】{module.player_intro}\n\n"
        f"【KP 指引】\n{guidance}"
    )


def render_node(node: ModuleNode) -> str:
    kind_part = f" kind:{node.kind}" if node.kind else ""
    parts = [f"【{node.title}】（id: {node.id}{kind_part}）{node.kp_text}"]
    if node.public_text:
        parts.append(f"公开摘要：{node.public_text}")
    for check in node.checks:
        parts.append(
            f"- 检定[{check.skill}]（{check.difficulty or '普通'}）"
            + (f" 前提：{check.prerequisite}" if check.prerequisite else "")
            + f"\n  成功：{check.on_success or '—'}\n  失败：{check.on_failure or '—'}"
            + (f"\n  大失败：{check.on_fumble}" if check.on_fumble else "")
        )
    for branch in node.branches:
        parts.append(f"- 分支[{branch.condition}]：{branch.outcome}")
        for sub in branch.then or []:
            parts.append(f"  - [{sub.condition}]：{sub.outcome}")
    if node.sub_node is not None:
        parts.append(f"（子环节 ↓）\n{render_node(node.sub_node)}")
    for child in node.sub_nodes:
        parts.append(f"（子节点 ↓）\n{render_node(child)}")
    if node.exits:
        parts.append(f"邻接（空间）：{'、'.join(node.exits)}")
    if node.contains:
        parts.append(f"包含：{'、'.join(node.contains)}")
    if node.leads_to:
        parts.append(f"情节推进：{'、'.join(node.leads_to)}")
    return "\n".join(parts)


def render_npc(npc: ModuleNpc) -> str:
    parts = [f"【{npc.name}】（id: {npc.id}）{npc.role or ''}", npc.kp_notes or ""]
    if npc.public_text:
        parts.append(f"公开摘要：{npc.public_text}")
    if npc.stats:
        parts.append("数据卡：" + "、".join(f"{k} {v}" for k, v in npc.stats.items()))
    if npc.same_as:
        parts.append(f"同一实体链接：{'、'.join(npc.same_as)}")
    for form in npc.forms:
        label = form.name or form.id
        line = f"形态[{form.id}] {label}"
        if form.notes:
            line += f"：{form.notes}"
        parts.append(line)
        if form.stats:
            parts.append("  形态数据：" + "、".join(f"{k} {v}" for k, v in form.stats.items()))
    return "\n".join(p for p in parts if p)


def render_endings(module: ScenarioModule) -> str:
    lines: list[str] = []
    for e in module.endings:
        # trigger 是提案③的可判定口径；有则并列展示，没有就不占位置。
        trigger_part = f"，触发：{e.trigger}" if e.trigger else ""
        lines.append(f"- {e.id}·{e.title}（条件：{e.condition or '—'}{trigger_part}）：{e.text}")
    return "\n".join(lines)


def render_agenda(module: ScenarioModule) -> str:
    """议程时间轴。每条：id · 标题（触发条件）→ kp_text，effects 缩进列出。"""
    if not module.agenda:
        return ""
    lines: list[str] = []
    for event in module.agenda:
        title = event.title or "（无标题）"
        lines.append(f"- {event.id} · {title}（{event.trigger}）→ {event.kp_text}")
        for effect in event.effects:
            lines.append(f"  · {effect}")
    return "\n".join(lines)


def render_opening(module: ScenarioModule) -> str:
    """开场脚本：场景 + 念白 + KP 要建立什么。"""
    if module.opening is None:
        return ""
    opening = module.opening
    parts: list[str] = []
    if opening.scene:
        parts.append(f"场景：{opening.scene}")
    parts.append(f"念白：{opening.script}")
    if opening.kp_notes:
        parts.append(f"KP 要建立：{opening.kp_notes}")
    return "\n".join(parts)


def render_visibility_pairs(module: ScenarioModule) -> str:
    """密级配对表：公开 ref ↔ 真相 ref。空则返回空串。"""
    if not module.visibility_pairs:
        return ""
    lines: list[str] = []
    for pair in module.visibility_pairs:
        note = f"（{pair.note}）" if pair.note else ""
        lines.append(f"- {pair.id}：公开 {pair.public_ref} ↔ 真相 {pair.secret_ref}{note}")
    return "\n".join(lines)


def render_full(module: ScenarioModule) -> str:
    """剧本全文：常驻 system prompt 用。

    为什么全文而不是"速查卡索引 + 工具按需查"：真实 DeepSeek 冒烟连跑三轮
    证明它的工具调用纪律靠 prompt 拽不动——整轮只调一次工具、NPC 名字现编、
    检定点视而不见。短模组全文也就几千 token，直接常驻比赌它"会去查"可靠。

    块顺序：overview → 开场脚本 → 议程时间轴（绝密）→ 密级配对（绝密）→
    调查节点 → NPC → 结局。空块整块省略——旧模组不含 opening/agenda/pairs 时
    输出不应变脏。
    """
    parts = [render_overview(module)]
    opening_text = render_opening(module)
    if opening_text:
        parts.append(f"═══ 【开场脚本】 ═══\n{opening_text}")
    agenda_text = render_agenda(module)
    if agenda_text:
        # 标题必须带「绝密」——议程 kp_text 里会出现真相关键词（怪物、地点），
        # 与 kp_truth 同级；提前泄露就毁了本模组。
        parts.append(f"═══ 【议程时间轴（绝密）】 ═══\n{agenda_text}")
    pairs_text = render_visibility_pairs(module)
    if pairs_text:
        parts.append(f"═══ 【密级配对（绝密）】 ═══\n{pairs_text}")
    nodes = "\n\n".join(render_node(n) for n in module.nodes)
    parts.append(f"═══ 调查节点 ═══\n\n{nodes}")
    npcs = "\n\n".join(render_npc(n) for n in module.npcs)
    parts.append(f"═══ 登场 NPC（专有名词以此为准，不得另起名字）═══\n\n{npcs}")
    parts.append(f"═══ 结局 ═══\n{render_endings(module)}")
    return "\n\n".join(parts)


def render_for_subject(
    module: ScenarioModule, *, sees_meta: bool, knows: Callable[[str], bool]
) -> str:
    """按主体视图渲染剧本（exec/14 P2）。

    - 守秘人（`sees_meta=True`）→ 直接返回 `render_full(module)`，**同一个函数、
      同一份输出**，不是等价副本。这是「重构不改行为」的一部分。
    - 其余主体 → 只给虚构内可见的部分：
      - **元层整块不给**：真相层 / 议程 / 结局 / 密级配对 / 开场 KP 指导 /
        节点的 `kp_text` / NPC 的 `kp_notes`。它们对**任何**虚构内主体永不可见。
      - 节点/NPC 只给 `public_text`（没有就只给名字，不拿 kp_text 顶替）。
      - 事实只给 `knows` 里、且 `tier=diegetic` 的那些。

    `knows` 传 `Subject.knows` 这类"给 fact_id 返回 bool"的可调用对象——只标
    `Callable[[str], bool]`、不 import `Subject`，避免 module_loader（数据层）
    反向依赖主体层。
    """
    if sees_meta:
        return render_full(module)

    parts: list[str] = [f"你身处的世界：{module.meta.title}"]
    if module.meta.era:
        parts[0] += f"（{module.meta.era}）"

    places: list[str] = []
    for node in _iter_all_nodes(module.nodes):
        text = (node.public_text or "").strip()
        places.append(f"- {node.title}：{text}" if text else f"- {node.title}")
    if places:
        parts.append("═══ 你知道的地方 ═══\n" + "\n".join(places))

    people: list[str] = []
    for npc in module.npcs:
        text = (npc.public_text or "").strip()
        role = f"（{npc.role}）" if npc.role else ""
        people.append(f"- {npc.name}{role}：{text}" if text else f"- {npc.name}{role}")
    if people:
        parts.append("═══ 你认识的人 ═══\n" + "\n".join(people))

    known = [f"- {f.text}" for f in module.facts if f.tier == "diegetic" and knows(f.id)]
    if known:
        parts.append("═══ 你知道的事 ═══\n" + "\n".join(known))
    return "\n\n".join(parts)


def _iter_all_nodes(nodes: list[ModuleNode]) -> list[ModuleNode]:
    out: list[ModuleNode] = []
    for node in nodes:
        out.append(node)
        if node.sub_node is not None:
            out.extend(_iter_all_nodes([node.sub_node]))
        out.extend(_iter_all_nodes(node.sub_nodes))
    return out
