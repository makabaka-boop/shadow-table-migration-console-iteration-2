# SQLite 影子表字段迁移

本项目包含：

- `frontend/`：React + Vite 页面，配置有限字段映射并展示预演失败行、源表修订号、**正式表代际**、正式表和只读历史版本；可在历史页对指定版本做**恢复预演**并确认恢复。
- `backend/`：FastAPI + SQLite 服务，复制到影子表、逐行校验、乐观修订号提交、同一事务切换正式表并保留只读旧版；恢复以只读历史版本为来源，按**正式表代际**裁决后在同一事务封存当前正式表、切换候选表并推进代际。

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
- 迁移预演还绑定所见的**正式表代际**（`formal_generation`，服务端保存、提交时核对）。若预演后正式表被恢复切换，旧迁移提交返回 `409 stale_generation`，不会覆盖刚恢复的内容。现有客户端提交字段保持兼容：`formal_generation` 为可选字段，缺省时仍以服务端保存的代际裁决。
- 提交时先复制旧正式表到历史版本，再构建带最终约束的待切换表。
- 正式表切换使用同一 SQLite 事务中的 `DROP TABLE records` 与 `ALTER TABLE ... RENAME TO records`。SQLite DDL 可事务回滚，因此复制中断、约束失败或切换失败都不会留下一半新一半旧的正式表。
- 历史版本元数据和行数据均通过 SQLite trigger 拒绝更新、删除或锁定后插入。
- `formal_ops` 台账与 `formal_generations` 代际表记录每次迁移/恢复的 `base_generation → new_generation` 血缘；台账对已结算行只增不改（trigger 拒绝 UPDATE/DELETE）。

### 历史版本恢复

- `POST /api/restores/preview`：在独立事务中把指定只读历史版本复制为带正式表全部约束（非空、主键、`code`/`legacy_id` 唯一）的候选表 `restore_pending_<preview_id>`，返回来源版本、**裁决代际**、源/候选行数，以及候选表与当前正式表的逐行差异（新增、删除、同 id 变化、未变）。预演不改写任何记录。
- `POST /api/restores/commit`：在一笔 `BEGIN IMMEDIATE` 事务内：核销预演与候选表 → 校验候选表仍与只读来源逐行一致 → 写入 `formal_ops`（kind=restore，记录来源版本与基代际）→ 封存当前正式表为新的锁定历史版本 → 条件推进代际（`UPDATE ... WHERE generation=base`，0 行即 `409 stale_generation`）→ `DROP/RENAME` 切换候选表 → 删除预演元数据。
- 约束失败、`restore_copy`/`restore_switch` 故障注入或代际过期都会整体回滚，并在事务外丢弃预演与候选表；旧预演不可重复提交（再次提交返回 `404 restore_preview_not_found` 或 `409 stale_generation`）。
- 历史版本始终只读：恢复只复制历史行，从不修改来源版本；恢复前的正式表完整封存，可供追查，且可再次作为恢复来源（恢复链）。

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
- 恢复预演的行数、约束、代际与差异报告，且预演不动正式表。
- 恢复确认在同一事务封存当前正式表、切换候选表、推进代际并一次性核销预演。
- **迁移与恢复交错**：旧迁移预演在恢复后提交被 `stale_generation` 拒绝；旧恢复预演在迁移后提交同样被拒绝。
- **两次恢复竞争**：串行竞争与基于独立 TestClient 的真实并发竞争，均只有一方成功，另一方回滚且无孤立版本/候选表，胜者预演不可重放。
- 恢复链（恢复一个由恢复封存的版本）、空版本恢复。
- `restore_copy`/`restore_switch` 故障注入后的整体回滚、旧预演作废与重新预演恢复。
- 服务重启后代际、血缘台账、历史版本行与只读性可查；`formal_ops` 台账只增不改。
