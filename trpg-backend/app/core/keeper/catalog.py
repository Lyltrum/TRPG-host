"""可玩模组目录：前端/API 列表 与 Keeper structured JSON 的对照表。

种子 Scenario 用固定 UUID；房间 `scenario_id` 命中本表时，Keeper 加载对应
`模组资料/*.structured.json`（gitignored，版权文件本地放置）。

仅列出**已有 structured 成品**、能真主持的模组。探针-only 的不进目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 与 seed 共用的稳定 id（勿随意改——前端 SCENARIO_REGISTRY 也钉死这些）
SCENARIO_BOOK_HUNTER_ID = "00000000-0000-0000-0000-000000000003"
SCENARIO_CORBITT_ID = "00000000-0000-0000-0000-000000000004"


@dataclass(frozen=True, slots=True)
class KeeperModuleSpec:
    scenario_id: str
    title: str
    # 相对 keeper_modules_dir 的文件名
    structured_filename: str
    version: str
    authors: tuple[str, ...]
    players_min: int
    players_max: int
    difficulty: int  # 1 入门 / 2 进阶 / 3 挑战
    estimated_duration: str
    # 给列表/选模组用的中性简介，不抄第三方剧情正文
    synopsis: str


# 权威目录：新增可玩模组只加这里 + 确保 模组资料/ 有 structured 文件
KEEPER_MODULE_SPECS: tuple[KeeperModuleSpec, ...] = (
    KeeperModuleSpec(
        scenario_id=SCENARIO_BOOK_HUNTER_ID,
        title="追书人",
        structured_filename="追书人.structured.json",
        version="1.0.0",
        authors=("本地剧本",),
        players_min=1,
        players_max=2,
        difficulty=1,
        estimated_duration="2-3 小时",
        synopsis="线性调查向短模组。失踪与藏书线索，适合 1–2 人试玩 AI 守秘人。",
    ),
    KeeperModuleSpec(
        scenario_id=SCENARIO_CORBITT_ID,
        title="科比特先生",
        structured_filename="科比特先生.structured.json",
        version="1.0.0",
        authors=("本地剧本",),
        players_min=1,
        players_max=4,
        difficulty=2,
        estimated_duration="3-5 小时",
        synopsis="宅邸调查向。邻居异常、报纸线索与宅邸探索；当前 AI 主持压测主模组。",
    ),
)


def spec_by_scenario_id(scenario_id: str | None) -> KeeperModuleSpec | None:
    if not scenario_id:
        return None
    for spec in KEEPER_MODULE_SPECS:
        if spec.scenario_id == scenario_id:
            return spec
    return None


def resolve_structured_path(modules_dir: Path, scenario_id: str | None) -> Path | None:
    """返回 structured JSON 绝对路径；无映射或文件不存在时返回 None。"""
    spec = spec_by_scenario_id(scenario_id)
    if spec is None:
        return None
    path = (modules_dir / spec.structured_filename).resolve()
    if not path.is_file():
        return None
    return path


def default_modules_dir() -> Path:
    """默认：仓库根下 `模组资料/`（trpg-backend 的上一级）。"""
    backend_root = Path(__file__).resolve().parents[3]  # app/core/keeper -> trpg-backend
    return (backend_root.parent / "模组资料").resolve()
