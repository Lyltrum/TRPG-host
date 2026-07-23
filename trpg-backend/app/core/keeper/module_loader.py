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


class ModuleEnding(_LenientModel):
    id: str
    title: str
    condition: str | None = None
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


def load_module(path: str | Path) -> ScenarioModule:
    """从结构化 JSON 文件加载模组。文件不存在/JSON 非法/形状不符都会抛异常——
    由调用方（build_narrator）决定回退策略，这里不吞。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return ScenarioModule.model_validate(raw)
