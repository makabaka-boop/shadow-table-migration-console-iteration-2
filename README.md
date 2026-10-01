# SQLite 影子表字段迁移

本项目包含：

- `frontend/`：React + Vite 页面，配置有限字段映射并展示预演失败行、源表修订号、正式表和历史版本。
- `backend/`：FastAPI + SQLite 服务，复制到影子表、逐行校验、乐观修订号提交、同一事务切换正式表并保留只读旧版。

## 支持的字段映射

- `copy`：复制旧表字段。
- `trim`：去除字符串首尾空白。
- `decimal_int`：仅接受十进制整数文本，并限制在 SQLite 有符号 64 位整数范围内。
- `constant`：写入常量；目标整数字段必须提供整数常量。

目标字段为 `id`、`code`、`label`，正式表还保留 `legacy_id`。新表约束包括：非空、整数范围、`id`、`code`、`legacy_id` 唯一。

## 运行

后端：

```bash
cd backend
python3 -m pip install -r requirements.txt
ALLOW_FAULT_INJECTION=1 \
PYTHONPATH=. \
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端开发服务器：

```bash
cd frontend
npm install
npm run dev
```

生产构建：

```bash
npm --prefix frontend run build
```

构建后 FastAPI 会在 `http://127.0.0.1:8000/` 直接提供 `frontend/dist` 页面。

## 一致性说明

- 预演在独立事务中创建带 `preview_id` 的影子表；约束失败或复制中断会回滚，不触碰正式表。
- 提交必须携带预演返回的 `source_revision`。提交事务使用 `BEGIN IMMEDIATE`，若预演后源表发生任何插入、更新或删除，提交返回 `409 stale_preview`，要求重新预演。
- 提交时先复制旧正式表到历史版本，再构建带最终约束的待切换表。
- 正式表切换使用同一 SQLite 事务中的 `DROP TABLE records` 与 `ALTER TABLE ... RENAME TO records`。SQLite DDL 可事务回滚，因此复制中断、约束失败或切换失败都不会留下一半新一半旧的正式表。
- 历史版本元数据和行数据均通过 SQLite trigger 拒绝更新、删除或锁定后插入。

## 测试

```bash
cd backend
PYTHONPATH=. python3 -m pytest tests -q
```

测试覆盖：

- trim、十进制解析、整数范围、非空、唯一、映射来源报告。
- 预演失败和复制中断后正式表/影子表状态。
- 提交切换前故障注入的事务回滚。
- 两个客户端交错写入与真正并发写入下的修订号拒绝。
- 数据库修订号、正式表内容、历史版本内容及只读 trigger。
