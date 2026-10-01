import React, { useEffect, useMemo, useState } from 'react';

const initialMappings = {
  id: { type: 'copy', source_column: 'legacy_id' },
  code: { type: 'copy', source_column: 'code' },
  label: { type: 'trim', source_column: 'raw_name' },
};

const mappingTypes = [
  { value: 'copy', label: '复制 copy' },
  { value: 'trim', label: '去首尾空白 trim' },
  { value: 'decimal_int', label: '十进制整数 decimal_int' },
  { value: 'constant', label: '常量 constant' },
];

const sourceColumns = ['legacy_id', 'code', 'raw_name', 'note'];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.error?.message || response.statusText || '请求失败';
    throw Object.assign(new Error(message), { status: response.status, data });
  }
  return data;
}

function MappingEditor({ mappings, onChange }) {
  const update = (field, patch) => {
    onChange({
      ...mappings,
      [field]: {
        ...mappings[field],
        // Selecting a mapping type must not leave a value from another type
        // attached to the mapping object sent to FastAPI.
        ...(patch.type && patch.type !== mappings[field].type ? { source_column: undefined, value: undefined } : {}),
        ...patch,
      },
    });
  };

  return (
    <div className="mapping-grid">
      {Object.entries(mappings).map(([field, mapping]) => (
        <label className="mapping-card" key={field}>
          <span className="field-name">新表字段：{field}</span>
          <select
            value={mapping.type}
            onChange={(event) => update(field, { type: event.target.value })}
          >
            {mappingTypes.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          {mapping.type === 'constant' ? (
            <input
              placeholder="常量值（id 字段需为整数）"
              value={mapping.value ?? ''}
              onChange={(event) => {
                const raw = event.target.value;
                update(field, { value: field === 'id' && raw !== '' ? Number(raw) : raw });
              }}
            />
          ) : (
            <select
              value={mapping.source_column || ''}
              onChange={(event) => update(field, { source_column: event.target.value })}
            >
              <option value="" disabled>选择旧表字段</option>
              {sourceColumns.map((column) => <option key={column} value={column}>{column}</option>)}
            </select>
          )}
        </label>
      ))}
    </div>
  );
}

function FailureTable({ failures }) {
  if (!failures?.length) return null;
  return (
    <section className="panel error-panel">
      <h2>预演失败行（{failures.length}）</h2>
      <p>以下问题逐行收集；本次预演没有生成可提交影子表，正式表保持不变。</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>旧表 rowid</th>
              <th>legacy_id</th>
              <th>字段</th>
              <th>失败原因</th>
              <th>映射来源</th>
              <th>影子值</th>
            </tr>
          </thead>
          <tbody>
            {failures.flatMap((failure) =>
              failure.errors.map((error, index) => (
                <tr key={`${failure.row_number}-${index}`}>
                  {index === 0 && <td rowSpan={failure.errors.length}>{failure.row_number}</td>}
                  {index === 0 && <td rowSpan={failure.errors.length}>{failure.legacy_id ?? 'NULL'}</td>}
                  <td>{error.field}</td>
                  <td><code>{error.code}</code><div>{error.message}</div></td>
                  <td>{error.mapping}</td>
                  <td><code>{JSON.stringify(failure.values?.[error.field] ?? null)}</code></td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DataTable({ title, rows, empty }) {
  return (
    <section className="panel">
      <h2>{title} <span>{rows.length} 行</span></h2>
      {rows.length === 0 ? <p>{empty}</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr>{Object.keys(rows[0]).map((key) => <th key={key}>{key}</th>)}</tr></thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>{Object.values(row).map((value, i) => <td key={i}>{JSON.stringify(value)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [state, setState] = useState(null);
  const [mappings, setMappings] = useState(initialMappings);
  const [preview, setPreview] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const [historyRows, setHistoryRows] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState('');
  const [faults, setFaults] = useState({ preview_copy: false, commit_switch: false });
  const [newLegacy, setNewLegacy] = useState({ legacy_id: '', code: '', raw_name: '', note: '' });

  const refresh = async () => setState(await api('/api/state'));

  useEffect(() => { refresh().catch((error) => setMessage({ type: 'error', text: error.message })); }, []);

  useEffect(() => {
    if (!selectedVersion) {
      setHistoryRows([]);
      return;
    }
    api(`/api/history/${selectedVersion}/rows`)
      .then((data) => setHistoryRows(data.rows))
      .catch((error) => setMessage({ type: 'error', text: error.message }));
  }, [selectedVersion, state]);

  const latestVersion = useMemo(() => state?.history?.at(-1)?.version_id || '', [state]);
  useEffect(() => {
    if (state && selectedVersion === '' && latestVersion) setSelectedVersion(String(latestVersion));
  }, [state, selectedVersion, latestVersion]);

  const runPreview = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await api('/api/migrations/preview', {
        method: 'POST',
        body: JSON.stringify(mappings),
      });
      setPreview(result);
      await refresh();
      if (result.ok) setMessage({ type: 'success', text: `预演通过，源表修订号 ${result.source_revision}` });
      else setMessage({ type: 'error', text: '预演发现约束或映射失败，未改变正式表' });
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const runCommit = async () => {
    if (!preview?.preview_id) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await api('/api/migrations/commit', {
        method: 'POST',
        body: JSON.stringify({ preview_id: preview.preview_id, source_revision: preview.source_revision }),
      });
      setMessage({ type: 'success', text: `提交成功：迁移 #${result.migration_id}，保留旧版 #${result.old_version.version_id}` });
      setPreview(null);
      await refresh();
    } catch (error) {
      if (error.status === 409 || error.status === 404) {
        setMessage({ type: 'error', text: `${error.message}。请重新预演后再提交。` });
        setPreview(null);
      } else {
        setMessage({ type: 'error', text: error.message });
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const addLegacyRow = async () => {
    setBusy(true);
    try {
      await api('/api/legacy', {
        method: 'POST',
        body: JSON.stringify({
          ...newLegacy,
          legacy_id: Number(newLegacy.legacy_id),
          raw_name: newLegacy.raw_name || null,
          note: newLegacy.note || null,
        }),
      });
      setNewLegacy({ legacy_id: '', code: '', raw_name: '', note: '' });
      setPreview(null);
      await refresh();
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const toggleFault = async (name) => {
    const enabled = !faults[name];
    try {
      await api('/api/test/faults', { method: 'POST', body: JSON.stringify({ name, enabled }) });
      setFaults({ ...faults, [name]: enabled });
    } catch (error) {
      setMessage({ type: 'error', text: `故障注入需以 ALLOW_FAULT_INJECTION=1 启动 API：${error.message}` });
    }
  };

  const reset = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api('/api/admin/reset', { method: 'POST' });
      setPreview(null);
      setSelectedVersion('');
      setHistoryRows([]);
      await refresh();
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <main className="app"><p>正在加载...</p></main>;

  return (
    <main className="app">
      <header>
        <div>
          <h1>SQLite 旧记录影子表迁移</h1>
          <p>有限字段映射：复制、trim、十进制整数解析、常量</p>
        </div>
        <div className="revision">源表修订号 <strong>{state.revision}</strong></div>
      </header>

      {message && <div className={`banner ${message.type}`}>{message.text}</div>}

      <section className="panel">
        <div className="panel-title">
          <h2>1. 字段映射与预演</h2>
          <div className="actions">
            <button disabled={busy} onClick={runPreview}>复制到影子表并预演</button>
            <button className="primary" disabled={busy || !preview?.preview_id} onClick={runCommit}>
              携带修订号提交
            </button>
          </div>
        </div>
        <MappingEditor mappings={mappings} onChange={setMappings} />
        {preview && (
          <div className={`preview ${preview.ok ? 'ok' : 'bad'}`}>
            <strong>{preview.ok ? '预演通过' : '预演失败'}</strong>
            <span>preview_id: {preview.preview_id || '未保留'}</span>
            <span>依据 source_revision: {preview.source_revision}</span>
            <span>读取行数: {preview.row_count}</span>
          </div>
        )}
      </section>

      <FailureTable failures={preview?.failures} />

      <section className="panel two-column">
        <div>
          <h2>2. 交错写入旧表</h2>
          <div className="inline-form">
            {Object.keys(newLegacy).map((key) => (
              <input
                key={key}
                placeholder={key}
                value={newLegacy[key]}
                onChange={(event) => setNewLegacy({ ...newLegacy, [key]: event.target.value })}
              />
            ))}
            <button onClick={addLegacyRow} disabled={busy}>插入并推进修订号</button>
          </div>
        </div>
        <div>
          <h2>故障注入 / 测试</h2>
          <div className="faults">
            <button onClick={() => toggleFault('preview_copy')}>{faults.preview_copy ? '关闭' : '开启'} 复制后中断</button>
            <button onClick={() => toggleFault('commit_switch')}>{faults.commit_switch ? '关闭' : '开启'} 切换前中断</button>
            <button onClick={reset}>重置数据库</button>
          </div>
          <p className="hint">影子表：{state.shadow_tables.length ? state.shadow_tables.join(', ') : '无'}</p>
        </div>
      </section>

      <DataTable title="旧表 legacy_records" rows={state.legacy} empty="暂无旧数据" />
      <DataTable title="正式表 records" rows={state.records} empty="尚未迁移；首次提交后生成正式表" />

      <section className="panel">
        <h2>只读历史版本</h2>
        {state.history.length === 0 ? <p>尚无保留版本。</p> : (
          <>
            <select value={selectedVersion} onChange={(event) => setSelectedVersion(event.target.value)}>
              {state.history.map((version) => (
                <option key={version.version_id} value={version.version_id}>
                  版本 #{version.version_id} / 迁移 {version.migration_id} / 源修订 {version.source_revision} / {version.row_count} 行
                </option>
              ))}
            </select>
            <DataTable title={`历史版本 #${selectedVersion} 内容`} rows={historyRows} empty="该版本为空" />
          </>
        )}
      </section>
    </main>
  );
}
