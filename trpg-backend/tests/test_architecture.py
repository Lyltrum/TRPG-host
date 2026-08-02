"""架构约束的自动守护（exec/27 阶段 0）。

## 为什么需要这个测试

`narrator ↔ keeper` 那 5 组循环依赖，**全靠三处函数内 import 掩盖着**——延迟导入
让它跑得起来，于是没有任何东西会变红，环就一直留着。

> 🔴 **没有测试守护的架构约束一定会退化。** 今天修好，两周后一句函数内 import
> 就能把它带回来。同项目一贯判据：能用代码确定性判断的，一律代码强制，别靠自觉。

所以这个测试用 AST 扫**全部** import，包括函数体内的延迟导入——那正是最容易
悄悄破坏架构的写法。

## 棘轮（ratchet）

现存的违规不是一天能修完的（`exec/27` 阶段 1 才修循环）。所以两份豁免清单先把
现状钉死：

- **清单里的**：已知违规，允许存在，但**不允许再多**；
- **清单外的**：新增一条就当场红。

而且清单**只能变短**：某条豁免修好后仍留在清单里，测试同样会红并要求删掉它。
这样清单自然收缩到空，不会变成垃圾堆。
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 分层。数字小的在下，**下层不许 import 上层**。
#:
#: 🔴 `app.core` 被拆成两截，因为它现在装着两类东西：
#: - 基础设施（db/errors/config/logging）：最底层，models 与 dto 都依赖它，正常；
#: - 领域逻辑（keeper/narrator/coc7_*）：在 models 之上、service 之下。
#: 第一版没分开，于是把 `from app.core.db import Base` 误判成 6 处违规。
_LAYERS: list[tuple[int, tuple[str, ...]]] = [
    (0, ("app.core.db", "app.core.errors", "app.core.config", "app.core.logging")),
    (1, ("app.models",)),
    (2, ("app.dto",)),
    (3, ("app.core",)),  # 领域逻辑：keeper / narrator / coc7_* / llm_tape …
    (4, ("app.service",)),
    (5, ("app.controller",)),
]

#: 已知的跨层违规（`exec/27` 待修）。key 是 (来源模块, 被依赖模块)。
#: 修好后必须从这里删掉，否则测试会提示"这条豁免已经不需要了"。
_LAYER_EXEMPTIONS: set[tuple[str, str]] = {
    # heartbeat 需要行动锁与 WS 广播——领域层反向依赖了 service。
    # 正解是把这两样抽成 core 侧的协议（Port），由 service 注入实现。
    ("app.core.keeper.heartbeat", "app.service.action_lock"),
    ("app.core.keeper.heartbeat", "app.service.ws_manager"),
}

#: 已知的循环依赖边。
#:
#: 🔴 **`exec/27` 阶段 1 之后这里是空的——请让它保持空。**
#:
#: 原先有两组环：
#: 1. `narrator.py` 同时是抽象层、工厂和一个实现，抽象反向依赖具体；
#: 2. `keeper/__init__.py` re-export `KeeperAgent`，而 `tools.py` 走
#:    `from app.core.keeper.primitives import dice`——`from 包 import 子模块` 会先执行包的
#:    `__init__`，于是 `tools → 包 → agent → tools`。
#:
#: 两组都靠**函数内 import / 加载顺序凑巧**撑着，不会有任何东西变红。真要往这里
#: 加条目之前，先确认不是"抽象层依赖了具体实现"或"包门面"这两种老毛病。
_CYCLE_EXEMPTIONS: set[tuple[str, str]] = set()


def _layer_of(module: str) -> tuple[int, str] | None:
    """返回 (层号, 匹配到的前缀)。取**最长**匹配——`app.core.db` 必须赢过 `app.core`。"""
    best: tuple[int, str] | None = None
    for level, prefixes in _LAYERS:
        for prefix in prefixes:
            matched = module == prefix or module.startswith(prefix + ".")
            if matched and (best is None or len(prefix) > len(best[1])):
                best = (level, prefix)
    return best


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(APP.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(path: pathlib.Path) -> set[str]:
    """这个文件 import 了哪些 `app.*` 模块。

    🔴 用 `ast.walk` 而不是只看顶层——**函数体内的延迟导入照样算**。
    那正是 `narrator.py` 用来绕过循环依赖的写法，也是这个测试最该抓住的东西。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {m for m in found if m.startswith("app.")}


def _build_graph() -> dict[str, set[str]]:
    """模块 → 它依赖的模块。被依赖方收敛到"有对应文件的模块"，
    这样 `from app.core.keeper.agent import X` 与 `import app.core.keeper.agent`
    落在同一个节点上。"""
    files = {_module_name(p): p for p in APP.rglob("*.py")}
    graph: dict[str, set[str]] = {}
    for mod, path in files.items():
        deps = set()
        for target in _imports_of(path):
            # 逐级向上找到真实存在的模块（`app.core.db` 的 `Base` 会被解析成
            # `app.core.db` 本身，而不是不存在的 `app.core.db.Base`）
            probe = target
            while probe and probe not in files:
                probe = probe.rpartition(".")[0]
            if probe and probe != mod:
                deps.add(probe)
        graph[mod] = deps
    return graph


def _find_cycles(graph: dict[str, set[str]]) -> set[tuple[str, str]]:
    """返回**参与循环的边**集合。

    报边而不是报环：一个环里通常只有一条边是"错的方向"，报边能直接指出该改哪里，
    而报整条环路只会让人对着一串模块名发呆。
    """
    color: dict[str, int] = {}
    stack: list[str] = []
    bad: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if color.get(dep, 0) == 0:
                visit(dep)
            elif color.get(dep) == 1:
                # 回边：从 node 指回仍在栈上的 dep，就是环里的那条边
                bad.add((node, dep))
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color.get(node, 0) == 0:
            visit(node)
    return bad


def test_no_new_circular_dependencies() -> None:
    """循环依赖只允许出现在豁免清单里。"""
    found = _find_cycles(_build_graph())
    unexpected = found - _CYCLE_EXEMPTIONS
    assert not unexpected, (
        "出现了新的循环依赖：\n"
        + "\n".join(f"  {a}  →  {b}" for a, b in sorted(unexpected))
        + "\n\n抽象层不该依赖具体实现。若确实无法避免，把它加进 "
        "_CYCLE_EXEMPTIONS 并在 exec/27 里说明理由。"
    )


def test_layers_do_not_invert() -> None:
    """下层不许 import 上层（models 不许碰 service，core 不许碰 controller…）。"""
    graph = _build_graph()
    violations: set[tuple[str, str]] = set()
    for mod, deps in graph.items():
        src = _layer_of(mod)
        if src is None:
            continue
        for dep in deps:
            dst = _layer_of(dep)
            if dst is None:
                continue
            if dst[0] > src[0]:
                violations.add((mod, dep))
    unexpected = violations - _LAYER_EXEMPTIONS
    assert not unexpected, (
        "出现了反向的跨层依赖：\n"
        + "\n".join(f"  {a}  →  {b}" for a, b in sorted(unexpected))
        + "\n\n依赖只能从上层指向下层。需要反过来用时，"
        "在下层定义协议（Port），由上层注入实现。"
    )


#: 垂直切出去的能力所在的包（exec/27 阶段 2）。
_CAPABILITIES_PKG = "app.core.keeper.capabilities"
_PRIMITIVES_PKG = "app.core.keeper.primitives"

#: 编排层：决定"何时问谁"，不该被任何一个能力反向依赖。
#: （阶段 5 它们会搬进 `runtime/`，那时这里换成一个前缀。）
_ORCHESTRATION = (
    "app.core.keeper.agent",
    "app.core.keeper.turn_executor",
    "app.core.keeper.heartbeat",
)


def _capability_of(module: str) -> str | None:
    """模块属于哪个能力。

    两个不算：包门面 `capabilities` 本身（它就是那个唯一认识所有能力的地方），
    以及**与能力代码同目录的测试文件**——测试要 import 编排层把整条链跑通，
    那是它的本职，不是架构违规。
    """
    prefix = _CAPABILITIES_PKG + "."
    if not module.startswith(prefix):
        return None
    parts = module[len(prefix) :].split(".")
    if parts[-1].startswith("test_"):
        return None
    return parts[0]


def test_capabilities_do_not_import_each_other() -> None:
    """🔴 一个能力 = 一个新人能单独读懂、单独改的目录。

    它一旦 import 了另一个能力，"只读一个目录"就不成立了，两个人也没法各改
    各的。真出现共用的东西，正确做法是**下沉**（`primitives/` 或 `deps.py`）
    或者承认边界切错了——`exec/27` 里 `checks` 被拆成 `skill_check`/`san_check`
    加 `primitives`，就是这条断言在设计阶段先抓出来的。
    """
    violations: set[tuple[str, str]] = set()
    for mod, deps in _build_graph().items():
        mine = _capability_of(mod)
        if mine is None:
            continue
        for dep in deps:
            theirs = _capability_of(dep)
            if theirs is not None and theirs != mine:
                violations.add((mod, dep))
    assert not violations, "能力之间互相 import 了：\n" + "\n".join(
        f"  {a}  →  {b}" for a, b in sorted(violations)
    )


def test_capabilities_do_not_import_the_orchestrator() -> None:
    """能力只被编排层调用，不反过来认识它——否则"加一个能力不改编排层"这条
    验收标准就没了着力点。"""
    violations: set[tuple[str, str]] = set()
    for mod, deps in _build_graph().items():
        if _capability_of(mod) is None:
            continue
        violations.update((mod, dep) for dep in deps if dep in _ORCHESTRATION)
    assert not violations, "能力反向依赖了编排层：\n" + "\n".join(
        f"  {a}  →  {b}" for a, b in sorted(violations)
    )


def test_primitives_never_know_about_capabilities() -> None:
    """规则原语是给能力用的，方向单向。反过来就说明那东西根本不是原语。"""
    graph = _build_graph()
    violations = {
        (mod, dep)
        for mod, deps in graph.items()
        if mod.startswith(_PRIMITIVES_PKG)
        for dep in deps
        if dep.startswith(_CAPABILITIES_PKG)
    }
    assert not violations, "原语依赖了能力：\n" + "\n".join(
        f"  {a}  →  {b}" for a, b in sorted(violations)
    )


def _runtime_imports_of(path: pathlib.Path) -> set[str]:
    """只算**运行时真的会执行**的 `app.*` import，跳过 `if TYPE_CHECKING:` 块。

    ⚠️ 这个放宽只用在下面那一条断言里。别处一律用 `_imports_of`——函数体内的
    延迟导入照样算，那正是当年用来绕过循环的写法。`TYPE_CHECKING` 块是**可证明
    不执行**的，构不成 import 环；把它算进去，`registry` 就只能放弃精确的类型
    标注（`Callable[..., str]`），代价落在每一个写能力的人身上。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            skip.update(id(child) for child in ast.walk(node) if child is not node)
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {m for m in found if m.startswith("app.")}


def test_registry_is_a_leaf_at_runtime() -> None:
    """注册表定义的是**机制**，能力都要 import 它。

    它只要在运行时碰一个 `app.*`，`contract → capabilities → contract` 就有
    成环的余地——阶段 1 刚用 `narration/contract.py` 交过一次学费，这里提前钉死。
    """
    assert _runtime_imports_of(APP / "core" / "keeper" / "registry.py") == set()


def test_exemption_lists_only_shrink() -> None:
    """🔴 棘轮：豁免清单里的条目**修好之后必须删掉**。

    没有这条，清单会变成只进不出的垃圾堆——过两个月没人知道哪些还成立、
    哪些早就修好了，于是它对新增违规也失去了拦截意义。
    """
    graph = _build_graph()
    cycles = _find_cycles(graph)
    stale_cycles = _CYCLE_EXEMPTIONS - cycles

    layer_violations: set[tuple[str, str]] = set()
    for mod, deps in graph.items():
        src = _layer_of(mod)
        if src is None:
            continue
        for dep in deps:
            dst = _layer_of(dep)
            if dst is not None and dst[0] > src[0]:
                layer_violations.add((mod, dep))
    stale_layers = _LAYER_EXEMPTIONS - layer_violations

    stale = stale_cycles | stale_layers
    assert not stale, "以下豁免已经不需要了，请从清单里删掉：\n" + "\n".join(
        f"  {a}  →  {b}" for a, b in sorted(stale)
    )
