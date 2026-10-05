# 冷链运输温度事件核验服务

基于 **Python + FastAPI + SQLite** 的冷链温度事件核验服务。客户端按设备上报温度读数，
服务自动核验并维护**超温区间**：同一设备的读数按（采样时间，事件号）排序后，
**连续两次有效读数超过可配置上限**才形成一个超温区间。

## 快速启动

### 方式一：Docker Compose（推荐，一键完成依赖安装与数据库初始化）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t coldchain-service .
docker run -d -p 8000:8000 -e TEMP_UPPER_LIMIT=8.0 \
  -v coldchain-data:/data --name coldchain coldchain-service
```

### 方式三：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库表结构在服务启动时自动创建，无需手工初始化。

### 访问地址

| 地址 | 说明 |
|---|---|
| http://localhost:8000 | 服务根地址（端口 `8000`） |
| http://localhost:8000/docs | Swagger 交互式 API 文档 |
| http://localhost:8000/openapi.json | OpenAPI 规范 |
| http://localhost:8000/api/health | 健康检查与当前生效配置 |

## 配置

通过环境变量配置（`docker-compose.yml` 中已给出默认值）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TEMP_UPPER_LIMIT` | `8.0` | 超温上限（℃）。温度**严格大于**该值才算超限 |
| `DB_PATH` | 容器内 `/data/coldchain.db`，本地 `./coldchain.db` | SQLite 数据库文件路径 |

修改示例：`TEMP_UPPER_LIMIT=2.0 docker compose up --build`

## 核心规则

1. **排序**：同一设备的读数按（采样时间，事件号）升序参与判定。
2. **区间形成**：连续两次有效读数超限才形成超温区间，**起点为首个超限读数**，
   区间以起点事件号标识。
3. **区间结束**：终点为起点之后**首次不超限**的读数；区间未结束时终点为 `null`。
4. **重算**：迟到、更正（`PUT`）、撤销（`DELETE`）读数后，自动重算该设备全部区间，
   响应中返回当前区间及本次**新增（added）/变更（changed）/失效（invalidated）**的区间；
   起点改变时旧区间失效、新区间新增。
5. **幂等与冲突**：重复提交同事件号且内容完全相同 → 幂等成功（HTTP 200，
   `duplicate: true`，不产生区间变更）；同事件号但内容不同 → HTTP 409 拒绝，
   需改用更正接口。
6. **校验**：无效时间格式、无效温度（非数字、NaN/Infinity、超出物理范围）
   → HTTP 422；更正/撤销不存在的目标 → HTTP 404。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/devices/{device_id}/readings` | 提交读数（成功 201，幂等重放 200，冲突 409） |
| `PUT` | `/api/devices/{device_id}/readings/{event_id}` | 按事件号更正采样时间和/或温度 |
| `DELETE` | `/api/devices/{device_id}/readings/{event_id}` | 按事件号撤销读数 |
| `GET` | `/api/devices/{device_id}/readings` | 查询设备读数（按采样时间、事件号排序） |
| `GET` | `/api/devices/{device_id}/intervals` | 查询设备当前超温区间 |
| `GET` | `/api/health` | 健康检查与生效配置 |

写操作（POST/PUT/DELETE）统一返回：

```json
{
  "device_id": "truck-01",
  "duplicate": false,
  "intervals":   [ "…该设备当前全部区间…" ],
  "added":       [ "…本次新增的区间…" ],
  "changed":     [ "…本次变更的区间（起点不变、终点变化）…" ],
  "invalidated": [ "…本次失效的区间…" ]
}
```

区间对象：`start_event_id`（区间标识）、`start_sampled_at`、`end_event_id`、
`end_sampled_at`（未结束时两者为 `null`）。

## 调用示例

以下假设服务运行于 `http://localhost:8000`，上限为默认 8.0℃。

### 1. 提交读数：单次超限不形成区间

```bash
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"r1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.2}'
```

```json
{"device_id":"truck-01","duplicate":false,"intervals":[],"added":[],"changed":[],"invalidated":[]}
```

### 2. 连续第二次超限：区间开启（起点 r1，终点为空）

```bash
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"r2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.6}'
```

```json
{
  "device_id": "truck-01",
  "duplicate": false,
  "intervals": [{"start_event_id":"r1","start_sampled_at":"2026-10-01T08:00:00Z",
                 "end_event_id":null,"end_sampled_at":null}],
  "added":     [{"start_event_id":"r1","start_sampled_at":"2026-10-01T08:00:00Z",
                 "end_event_id":null,"end_sampled_at":null}],
  "changed": [],
  "invalidated": []
}
```

### 3. 首次不超限读数：区间闭合（changed 中出现该区间）

```bash
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"r3","sampled_at":"2026-10-01T08:20:00Z","temperature":4.0}'
```

`changed` 中返回：`{"start_event_id":"r1", ..., "end_event_id":"r3","end_sampled_at":"2026-10-01T08:20:00Z"}`。

### 4. 迟到读数触发重算

在 r1 与 r2 之间补一条正常读数，原区间失效：

```bash
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"r1b","sampled_at":"2026-10-01T08:05:00Z","temperature":4.0}'
```

`invalidated` 中将包含原区间（起点 r1）。

### 5. 幂等重放与冲突拒绝

```bash
# 完全相同的内容重复提交 → HTTP 200，duplicate=true，区间不变
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"r1b","sampled_at":"2026-10-01T08:05:00Z","temperature":4.0}'

# 同事件号、不同内容 → HTTP 409
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"r1b","sampled_at":"2026-10-01T08:05:00Z","temperature":9.9}'
```

### 6. 更正读数（可只更正温度或只更正时间）

```bash
curl -s -X PUT http://localhost:8000/api/devices/truck-01/readings/r2 \
  -H 'Content-Type: application/json' \
  -d '{"temperature":3.0}'
```

### 7. 撤销读数

```bash
curl -s -X DELETE http://localhost:8000/api/devices/truck-01/readings/r1b
```

### 8. 查询读数与区间

```bash
curl -s http://localhost:8000/api/devices/truck-01/readings
curl -s http://localhost:8000/api/devices/truck-01/intervals
```

### 9. 错误示例

```bash
# 无效时间 → HTTP 422
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"rx","sampled_at":"not-a-time","temperature":5.0}'

# 无效温度 → HTTP 422
curl -s -X POST http://localhost:8000/api/devices/truck-01/readings \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"rx","sampled_at":"2026-10-01T08:00:00Z","temperature":"hot"}'

# 更正/撤销不存在的目标 → HTTP 404
curl -s -X PUT http://localhost:8000/api/devices/truck-01/readings/nope \
  -H 'Content-Type: application/json' -d '{"temperature":5.0}'
curl -s -X DELETE http://localhost:8000/api/devices/truck-01/readings/nope
```

## 说明与约定

- 采样时间接受 ISO 8601 格式（如 `2026-10-01T08:00:00Z`、`2026-10-01T16:00:00+08:00`），
  无时区信息时按 UTC 处理；存储与输出统一归一化为 UTC。
- 事件号在同一设备内唯一；不同设备之间相互隔离，可使用相同事件号。
- 温度合法范围为 [-273.15, 500] ℃，且不允许 NaN/Infinity。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由
  service.py   # 区间计算、重算与差异对比（核心领域逻辑）
  models.py    # 请求/响应模型与校验
  db.py        # SQLite 连接与建表
  config.py    # 环境变量配置
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
