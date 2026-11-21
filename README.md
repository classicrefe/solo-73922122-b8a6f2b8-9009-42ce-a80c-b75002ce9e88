# 联运改单对账演示（FastAPI + SQLite）

联运途中发生拆单、并单后，月底对账仍按**同一批货的重量**取数。本应用让数据员录入原单、
提交改单包，并保证改单以“整包切换”的方式生效；可查看挂起/拒收原因，并下载只含当前
有效单的对账 CSV。

## 业务规则

| 规则 | 实现行为 |
| --- | --- |
| 运单 = 单号 + 各货批整数公斤数 | `waybills` / `batches` 两张表，重量要求正整数 |
| 改单包以一组新单替换一组旧单 | 包内 `old_waybill_nos` → `new_waybills`，整包在一个事务内切换 |
| 逐货批重量相等才可切换 | 旧单集合与新单集合的 `货批号 → 重量` 映射必须完全一致，否则整包拒收 |
| 新单号从未生效才可使用 | 新单号在运单表中出现过即拒收（**含已被替换的历史单，不可复用**） |
| 旧单未到齐 | 包置为 `suspended` 并记录缺失单号；补齐原单后在同一写事务中自动续办 |
| 任一旧单已被替换 | 整包 `rejected`，其他单据不受影响 |
| 并行提交，同一旧单最多被替换一次 | 进程内写锁 + `BEGIN IMMEDIATE` 串行化所有写事务，竞争失败的包拒收 |
| 同包号同内容重传 | 沿用原处理结果（HTTP 200，`replayed=true`），不改变任何状态 |
| 同包号异内容重传 | HTTP 409 整包拒收，原处理结果保留 |
| 对账 CSV | 仅导出 `status=active` 的运单货批，已替换单不出现 |

校验优先级：① 旧单已被替换 → 拒收；② 旧单缺失 → 挂起；③ 新单号冲突 → 拒收；
④ 逐货批重量不一致 → 拒收；全部通过 → 整包生效。

## 目录结构

```
app/
  database.py   # SQLite 连接、建表、串行化写事务
  schemas.py    # Pydantic 请求/响应模型与结构校验
  services.py   # 业务规则：录入、校验、挂起/续办、整包切换、幂等指纹
  main.py       # FastAPI 路由、异常处理、CSV 导出
  static/index.html  # 单页演示界面（原生 JS，5 秒自动刷新）
tests/          # pytest：18 个用例（服务层 + API 层，含并发与幂等）
docker-compose.yml / Dockerfile
```

## 用 Docker Compose 启动（推荐演示方式）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据文件持久化在宿主机 `./data/waybills.db`（compose 挂载 `/data`）。
停止后数据保留，`docker compose down` 不会删除 `./data`。

## 本地直接运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库路径由环境变量 `WAYBILL_DB_PATH` 控制，默认 `./data/waybills.db`。
交互文档：http://localhost:8000/docs 。

## 自测

```bash
pip install -r requirements-dev.txt
pytest -q
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 录入原单（重复单号 409） |
| GET | `/api/waybills?status=active|replaced` | 运单台账 |
| POST | `/api/packages` | 提交改单包；首次 201，同内容重传 200，异内容 409 |
| GET | `/api/packages?status=applied|suspended|rejected` | 处理结果与挂起/拒收原因 |
| GET | `/api/reconciliation.csv` | 仅含有效单的对账 CSV（带 UTF-8 BOM，Excel 可直接打开） |
| GET | `/api/stats` | 有效/替换单数、有效货批总重量、各状态包数量 |

### 请求示例

```json
POST /api/packages
{
  "package_no": "P1",
  "old_waybill_nos": ["W1", "W2"],
  "new_waybills": [
    {"waybill_no": "N1", "batches": [{"batch_no": "B1", "weight_kg": 100}]},
    {"waybill_no": "N2", "batches": [{"batch_no": "B2", "weight_kg": 200}]}
  ]
}
```

旧单只传单号（货批重量以库中已录入的数据为准）；新单必须带全量货批。
内容指纹为规范化 JSON（排序键、排序列表）的 SHA-256，字段提交顺序不同也视为同内容。

## 演示脚本（浏览器）

1. 录入原单 `W1`（B1=100, B2=200）、`W2`（B3=300）。
2. 提交拆单包 `P1`：旧 `W1` → 新 `N1`(B1=100)、`N2`(B2=200)，即时生效。
3. 提交挂起包 `P2`：旧 `W2,W4` → 新 `N3`(B3=300,B4=40)，页面显示挂起原因“等待 W4”。
4. 同内容重传 `P2` 提示沿用原结果；改一个重量再传则 409 拒收。
5. 录入原单 `W4`（B4=40），`P2` 自动续办生效。
6. 再引用已替换的 `W1` 提交包，看到整包拒收及原因。
7. 下载对账 CSV，仅包含 `N1/N2/N3` 等当前有效单，总重量与原批次一致。
