# 联运改单对账演示（FastAPI + SQLite）

联运途中发生拆单、并单后，月底对账仍按**同一批货的重量**取数。本应用让数据员录入原单、
提交改单包，并保证改单以“整包切换”的方式生效；可查看挂起/拒收原因，并下载只含当前
有效单的对账 CSV。浏览器里还可上传承运方月账单 CSV，按同一台账快照逐行核对，并找出
旧单、新单重复计费的疑似行。

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
| 账单核对 | 整份账单在同一个只读快照事务中核对；沿已生效拆并记录从现行单/历史单回追原单货批 |
| 账单异常 | 单号/货批查无、计费重量不符、沿途拆并前后总重不等 → 待人工核实并说明原因 |
| 疑似重复 | 两行及以上共同覆盖同一条原单货批 → 都标为疑似重复，列账单行号与共同原单货批 |
| 同号货批 | 来自彼此无关的原单时可分别通过，不作为重复 |

校验优先级：① 旧单已被替换 → 拒收；② 旧单缺失 → 挂起；③ 新单号冲突 → 拒收；
④ 逐货批重量不一致 → 拒收；全部通过 → 整包生效。

## 目录结构

```
app/
  database.py   # SQLite 连接、建表、串行化写事务、只读快照事务
  schemas.py    # Pydantic 请求/响应模型与结构校验
  services.py   # 业务规则：录入、校验、挂起/续办、整包切换、幂等指纹
  reconciliation.py  # 承运方账单：CSV 解析、拆并链回追、逐行判定、重复识别
  main.py       # FastAPI 路由、异常处理、CSV 导出、账单核对接口
  static/index.html  # 单页演示界面（原生 JS，5 秒自动刷新）
tests/          # pytest：46 个用例（服务层 + 账单核对 + API 层，含并发与幂等）
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
| GET | `/api/bill-checks/sample.csv` | 下载演示样例账单（首行重复申报一次） |
| POST | `/api/bill-checks` | 上传承运方账单 CSV（multipart 字段 `file`），返回逐行核对结果 |
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

### 账单核对请求

```
POST /api/bill-checks
Content-Type: multipart/form-data
file=@bill.csv
```

账单 CSV 表头至少包含 `运单号`、`货批号`、`计费公斤数`（也接受英文
`waybill_no` / `batch_no` / `weight_kg`；分隔符支持逗号、分号、制表符；
编码支持 UTF-8（含 BOM）、UTF-16、GB18030）。每行申报对应运单货批的**全重**。

```json
{
  "filename": "bill.csv",
  "checked_at": "2026-11-21T02:41:36+00:00",
  "ledger_state": {
    "token": "db862d5d…", "waybill_count": 5, "active_waybill_count": 2,
    "batch_count": 7, "package_count": 2, "applied_package_count": 2
  },
  "summary": {
    "total": 7, "passed": 2, "manual_review": 3,
    "suspected_duplicate": 2, "duplicate_groups": 1
  },
  "rows": [
    {
      "line_no": 2,
      "waybill_no": "N9", "batch_no": "B1", "billed_weight_kg": "100",
      "ledger_weight_kg": 100, "waybill_status": "active",
      "status": "suspected_duplicate",
      "reasons": [],
      "original_batch": {"waybill_no": "W1", "batch_no": "B1", "weight_kg": 100},
      "trace": [
        {"waybill_no": "W1", "batch_no": "B1", "waybill_status": "replaced",
         "via_package_no": null},
        {"waybill_no": "N1", "batch_no": "B1", "waybill_status": "replaced",
         "via_package_no": "P1"},
        {"waybill_no": "N9", "batch_no": "B1", "waybill_status": "active",
         "via_package_no": "P2"}
      ],
      "duplicate_with": [
        {"line_no": 3, "waybill_no": "W1", "batch_no": "B1"}
      ],
      "shared_original_batches": [
        {"waybill_no": "W1", "batch_no": "B1", "weight_kg": 100}
      ]
    }
  ],
  "duplicate_groups": [
    {
      "original_waybill_no": "W1", "original_batch_no": "B1", "weight_kg": 100,
      "line_numbers": [2, 3],
      "rows": [
        {"line_no": 2, "waybill_no": "N9", "batch_no": "B1",
         "billed_weight_kg": "100"},
        {"line_no": 3, "waybill_no": "W1", "batch_no": "B1",
         "billed_weight_kg": "100"}
      ]
    }
  ]
}
```

`line_no` 是 CSV 中的物理行号（表头为第 1 行，跳过的空行仍占行号），便于财务
直接定位。行状态：

| status | 含义 |
| --- | --- |
| `passed` | 单号、货批、计费重量均与台账一致，且可唯一回追到原单货批 |
| `manual_review` | 查无单号/货批、计费重量不符、重量格式非法、拆并记录缺失/歧义，或沿途拆并前后总重不等；`reasons` 给出原因 |
| `suspected_duplicate` | 与另一行（或多行）共同覆盖同一条原单货批；`duplicate_with` 给出对方账单行号与运单/货批，`shared_original_batches` 给出共同原单货批 |

判定顺序：先做逐行核对，通过的行再参与重复识别（待人工核实的行不参与，
避免把坏行混入重复结论）。

## 演示脚本（浏览器）

1. 录入原单 `W1`（B1=100, B2=200）、`W2`（B3=300）。
2. 提交拆单包 `P1`：旧 `W1` → 新 `N1`(B1=100)、`N2`(B2=200)，即时生效。
3. 提交挂起包 `P2`：旧 `W2,W4` → 新 `N3`(B3=300,B4=40)，页面显示挂起原因“等待 W4”。
4. 同内容重传 `P2` 提示沿用原结果；改一个重量再传则 409 拒收。
5. 录入原单 `W4`（B4=40），`P2` 自动续办生效。
6. 再引用已替换的 `W1` 提交包，看到整包拒收及原因。
7. 下载对账 CSV，仅包含 `N1/N2/N3` 等当前有效单，总重量与原批次一致。
8. 在“⑤ 承运方月账单核对”里上传账单 CSV（可先点“下载样例账单”），查看
   通过/待人工核实/疑似重复三类结论、原单货批与追溯路径。
