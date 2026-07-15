import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, createTrpgSdk, type Example } from 'trpg-sdk';

// SDK 客户端只需要构造一次（放在组件外面，不要放进组件函数体，否则每次
// 重新渲染都会重建一个新实例）。baseUrl 从 .env 里的 VITE_API_BASE_URL 读，
// 读不到时兜底指向本地默认端口，方便没配置 .env 也能跑起来。
const sdk = createTrpgSdk({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'
});

/** 把 catch 到的异常转成一句展示给用户的话：SDK 抛出的 ApiError 用它自带的
 * message（后端返回的具体错误原因，比如"同名示例已存在"）；其他情况
 * （网络断了之类）用调用方传入的兜底文案。 */
function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/**
 * 演示页面：通过 trpg-sdk 调用后端 /api/v1/examples，跑一遍完整的
 * 新增 / 列表 / 行内编辑 / 删除，验证前后端端到端联调没问题。
 * 跟真实业务无关，纯粹是这套骨架的使用范例。
 */
export default function App() {
  // 列表数据 + 页面级状态
  const [examples, setExamples] = useState<Example[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // "新增"表单的两个输入框
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');

  // "编辑"状态：editingId 非空时，对应那一行会切换成行内编辑表单
  // （而不是用浏览器原生 window.prompt——那种方式在部分自动化/受限环境下
  // 不会弹出，而且本身也不是很好的交互）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');

  /** 从后端重新拉一遍完整列表，创建/编辑/删除成功之后都会调用它来刷新页面。 */
  const loadExamples = async () => {
    setLoading(true);
    setError(null);
    try {
      setExamples(await sdk.examples.list());
    } catch (err) {
      setError(errorMessage(err, '加载失败'));
    } finally {
      setLoading(false);
    }
  };

  // 组件首次挂载时加载一次列表；依赖数组是空的，只在 mount 时跑一次。
  useEffect(() => {
    void loadExamples();
  }, []);

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    setError(null);
    try {
      // 描述是可选字段，输入框留空时传 null 而不是空字符串，跟后端的
      // Optional[str] 语义对齐。
      await sdk.examples.create({ name, description: description || null });
      setName('');
      setDescription('');
      await loadExamples();
    } catch (err) {
      setError(errorMessage(err, '创建失败'));
    }
  };

  /** 点击"编辑"：把编辑表单的初始值设成这一条记录当前的值，并标记正在编辑它。 */
  const startEditing = (example: Example) => {
    setEditingId(example.id);
    setEditName(example.name);
    setEditDescription(example.description ?? '');
  };

  const cancelEditing = () => {
    setEditingId(null);
    setEditName('');
    setEditDescription('');
  };

  const handleSaveEdit = async (event: FormEvent) => {
    event.preventDefault();
    if (!editingId || !editName.trim()) return;
    setError(null);
    try {
      await sdk.examples.update(editingId, {
        name: editName,
        description: editDescription || null
      });
      cancelEditing();
      await loadExamples();
    } catch (err) {
      setError(errorMessage(err, '更新失败'));
    }
  };

  const handleDelete = async (example: Example) => {
    try {
      await sdk.examples.remove(example.id);
      await loadExamples();
    } catch (err) {
      setError(errorMessage(err, '删除失败'));
    }
  };

  return (
    <main className="shell">
      <header className="hero">
        <p className="eyebrow">TRPG-master</p>
        <h1>Examples 端到端联调</h1>
        <p className="lede">
          通过 trpg-sdk 调用后端 /api/v1/examples 接口，验证前后端全链路 CRUD 流程。
        </p>
      </header>

      {/* 新增表单：GET/POST/PUT/DELETE 里的 POST */}
      <form className="panel example-form" onSubmit={handleCreate}>
        <h2>新增 Example</h2>
        <div className="example-form-row">
          <input
            placeholder="名称"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <input
            placeholder="描述（可选）"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <button type="submit">新增</button>
        </div>
        {error && <p className="example-error">{error}</p>}
      </form>

      {/* 列表：每一行要么是普通展示态，要么（当 editingId 命中这一行时）
          切换成行内编辑表单——两种渲染分支写在同一个三元表达式里。 */}
      <section className="panel">
        <h2>Example 列表{loading ? '（加载中…）' : ''}</h2>
        {examples.length === 0 && !loading && <p className="lede">暂无数据，先新增一条。</p>}
        <ul className="example-list">
          {examples.map((example) =>
            editingId === example.id ? (
              <li key={example.id} className="example-item">
                <form className="example-edit-form" onSubmit={handleSaveEdit}>
                  <input
                    value={editName}
                    onChange={(event) => setEditName(event.target.value)}
                  />
                  <input
                    placeholder="描述（可选）"
                    value={editDescription}
                    onChange={(event) => setEditDescription(event.target.value)}
                  />
                  <div className="example-actions">
                    <button type="submit">保存</button>
                    <button type="button" onClick={cancelEditing}>
                      取消
                    </button>
                  </div>
                </form>
              </li>
            ) : (
              <li key={example.id} className="example-item">
                <div>
                  <strong>{example.name}</strong>
                  {example.description && (
                    <span className="example-desc"> — {example.description}</span>
                  )}
                </div>
                <div className="example-actions">
                  <button type="button" onClick={() => startEditing(example)}>
                    编辑
                  </button>
                  <button type="button" onClick={() => handleDelete(example)}>
                    删除
                  </button>
                </div>
              </li>
            )
          )}
        </ul>
      </section>
    </main>
  );
}
