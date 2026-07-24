"""结构化剧本数据的加载（keeper agent 实验）。

剧本文件是从模组 PDF 人工结构化出来的 JSON（如 `模组资料/追书人.structured.json`），
**gitignore、不进公开仓库**——第三方模组的版权不属于本项目。这里只提交
「形状定义 + 加载器」；测试用 `tests/fixtures/` 下的原创迷你剧本。

形状刻意宽松（大量 Optional / 自由文本字段）：不同模组的组织方式千差万别，
这一版的目标是「让 agent 能按需查阅」，不是「把所有模组抽象成统一状态机」——
后者是最终目的（所有模组可抽象、AI 能推进），但抽象要从多个真实模组的共性里
长出来，第一个模组先忠实还原。
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class _LenientModel(BaseModel):
    # 剧本 JSON 里允许出现形状定义之外的字段（不同模组会有各自的自定义键），
    # 忽略而不是报错——加载器的职责是"读得出来"，不是"校验模组写法"。
    model_config = ConfigDict(extra="ignore")


class ModuleCheck(_LenientModel):
    """一次检定点：技能、难度与成败后果（都是给 KP 看的自由文本）。"""

    skill: str
    difficulty: str | None = None
    on_success: str | None = None
    on_failure: str | None = None
    on_fumble: str | None = None
    prerequisite: str | None = None


class ModuleBranch(_LenientModel):
    """节点内的条件分支（"若玩家杀死人影→…"）。`then` 允许嵌套一层。"""

    condition: str
    outcome: str
    then: list["ModuleBranch"] | None = None


class ModuleNode(_LenientModel):
    """一个调查节点/场景：KP 视角的完整信息 + 检定点 + 通向哪里。"""

    id: str
    title: str
    kp_text: str
    checks: list[ModuleCheck] = []
    branches: list[ModuleBranch] = []
    sub_node: "ModuleNode | None" = None
    leads_to: list[str] = []


class ModuleNpc(_LenientModel):
    id: str
    name: str
    role: str | None = None
    kp_notes: str | None = None
    # 数据卡保持自由字典：不同怪物/NPC 的数据轴不一样，agent 直接读原文。
    stats: dict | None = None


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

    def node_by_id(self, node_id: str) -> ModuleNode | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
            if node.sub_node is not None and node.sub_node.id == node_id:
                return node.sub_node
        return None

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


def load_module(path: str | Path) -> ScenarioModule:
    """从结构化 JSON 文件加载模组。文件不存在/JSON 非法/形状不符都会抛异常——
    由调用方（build_narrator）决定回退策略，这里不吞。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return ScenarioModule.model_validate(raw)


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
    parts = [f"【{node.title}】（id: {node.id}）{node.kp_text}"]
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
    if node.leads_to:
        parts.append(f"通向：{'、'.join(node.leads_to)}")
    return "\n".join(parts)


def render_npc(npc: ModuleNpc) -> str:
    parts = [f"【{npc.name}】（id: {npc.id}）{npc.role or ''}", npc.kp_notes or ""]
    if npc.stats:
        parts.append("数据卡：" + "、".join(f"{k} {v}" for k, v in npc.stats.items()))
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


def render_full(module: ScenarioModule) -> str:
    """剧本全文：常驻 system prompt 用。

    为什么全文而不是"速查卡索引 + 工具按需查"：真实 DeepSeek 冒烟连跑三轮
    证明它的工具调用纪律靠 prompt 拽不动——整轮只调一次工具、NPC 名字现编、
    检定点视而不见。短模组全文也就几千 token，直接常驻比赌它"会去查"可靠。

    块顺序：overview → 开场脚本 → 议程时间轴（绝密）→ 调查节点 → NPC → 结局。
    空块整块省略——旧模组不含 opening/agenda 时输出不应变脏。
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
    nodes = "\n\n".join(render_node(n) for n in module.nodes)
    parts.append(f"═══ 调查节点 ═══\n\n{nodes}")
    npcs = "\n\n".join(render_npc(n) for n in module.npcs)
    parts.append(f"═══ 登场 NPC（专有名词以此为准，不得另起名字）═══\n\n{npcs}")
    parts.append(f"═══ 结局 ═══\n{render_endings(module)}")
    return "\n\n".join(parts)
