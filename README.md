# 爆破振速持续超限复核 API

纯后端 HTTP 服务。安全复核员一次提交同一测点的一批振速采样（传输乱序亦可），
服务先按时间升序重排，再识别**持续超限事件**，给出整体结论：

- **放行**：没有任何合格的持续超限事件（短暂尖峰不会误判为持续超限）；
- **复核**：存在至少一个合格事件。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · pytest。

## 判定口径

### 采样合法性（任一不满足，整批返回 `422`，不输出部分结果）

- `timestamp`：必须是以 `Z` 结尾、**精确到秒**的 RFC3339，如 `2026-09-15T10:00:00Z`；
  不接受 `+00:00` 等偏移写法，不接受小数秒；
- `vibration`：单位 **mm/s**，有限数值，范围 `0.00 ~ 200.00`
  （`NaN` / `Infinity` / `-Infinity` 一律拒绝）；数字必须使用普通十进制写法，
  **小数位至多 2 位、不得使用科学计数法**——校验按 JSON 原始字面量执行，
  因此 `5.000`、`5.005`、`5e0`、`1.2E1` 即使数值落在范围内也会被拒绝，
  `5`、`5.0`、`5.00`、`200.00` 均合法；
- 重排后时间戳**不得重复**；
- 重排后**相邻采样间隔必须恰为 1 秒**。

### 事件识别

1. 振速 **`>= 5.00` mm/s** 记为超限；
2. 连续超限采样 **不少于 3 个**才构成合格区段；
3. 两个合格区段之间：
   - 只隔 **1 或 2 个**低值采样（`< 5.00`）→ **合并**为一个事件；
   - 间隔 **至少 3 个**采样 → **分开**为两个事件；
   - 中间夹有长度不足 3 的高值短段时，两合格段端点间隔必然 ≥ 3，自然分开，短段不并入；
4. 事件 `start` / `end` 取**两端超限采样**的时间（合并时中间的低值采样不计入起止）；
5. 持续秒数 = 首尾时间差（秒）**+ 1 秒**；
6. 峰值取区间内最大振速；**并列时取最早出现的时刻**。

事件按开始时间升序列出。

### 累计超限量（可选）

请求体带 `"include_exposure": true` 时，每个事件额外返回 `excess_dose_mm`
（单位 mm）：从事件**首个超限采样到末个超限采样**逐秒累加
`max(振速 − 5.00, 0) × 1 秒`。合并区段中夹着的低值采样贡献为零；
全程十进制定点运算并保留两位小数，避免浮点累计漂移。
省略该字段或为 `false` 时响应结构不变；非布尔取值按请求校验错误返回 `422`。

## API

### `POST /api/v1/analyze`

请求体：

```json
{
  "samples": [
    {"timestamp": "2026-09-15T10:00:02Z", "vibration": 5.1},
    {"timestamp": "2026-09-15T10:00:00Z", "vibration": 5.0},
    {"timestamp": "2026-09-15T10:00:01Z", "vibration": 7.0},
    {"timestamp": "2026-09-15T10:00:03Z", "vibration": 4.99},
    {"timestamp": "2026-09-15T10:00:04Z", "vibration": 6.2},
    {"timestamp": "2026-09-15T10:00:05Z", "vibration": 8.8},
    {"timestamp": "2026-09-15T10:00:06Z", "vibration": 5.0}
  ],
  "include_exposure": true
}
```

`include_exposure` 可选，省略或为 `false` 时响应结构与旧版一致。上例乱序提交；
重排后为「3 个超限 + 1 个低值 + 3 个超限」，间隔 1 个低值，合并为一个事件：

```json
{
  "conclusion": "复核",
  "event_count": 1,
  "events": [
    {
      "start": "2026-09-15T10:00:00Z",
      "end": "2026-09-15T10:00:06Z",
      "duration_seconds": 7,
      "peak_vibration": 8.8,
      "peak_timestamp": "2026-09-15T10:00:05Z",
      "excess_dose_mm": 7.1
    }
  ]
}
```

其中 `excess_dose_mm` = (0.00+2.00+0.10) + 0.00（低值）+ (1.20+3.80+0.00) = 7.10 mm。

无合格事件时：

```json
{ "conclusion": "放行", "event_count": 0, "events": [] }
```

整批不合法时返回 `422`，例如重复时间戳：

```json
{ "detail": "时间戳重复：2026-09-15T10:00:01Z", "code": "duplicate_timestamp" }
```

其他错误码：`non_second_interval`（相邻间隔不为 1 秒）、
`invalid_number_format`（数字写法不合法：超过两位小数、科学计数法、NaN/Infinity）、
`invalid_json`（请求体不是合法 JSON）、
`invalid_request`（字段缺失、时间戳格式、数值越界、类型不符等）。

另有 `GET /health` 存活探针，返回 `{"status":"ok"}`。服务启动后可访问
`/docs` 查看交互式接口文档。

### curl 示例

```bash
curl -X POST http://localhost:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"samples":[{"timestamp":"2026-09-15T10:00:00Z","vibration":5.00},
                  {"timestamp":"2026-09-15T10:00:01Z","vibration":6.10},
                  {"timestamp":"2026-09-15T10:00:02Z","vibration":5.30}]}'
```

## 运行方式

### Docker Compose（推荐）

默认只启动 API，宿主端口为 8000：

```bash
docker compose up --build
```

用 `API_PORT` 覆盖宿主端口：

```bash
API_PORT=9000 docker compose up --build
# API 实际监听 http://localhost:9000
```

### 一次性验收服务 verify

`verify` 是一次性服务：与 `api` 共用同一个镜像（镜像只在 `api` 上声明一次
构建，`verify` 只引用镜像名、不重复构建，避免并行构建同名 tag 冲突），
启动时先拉起 `api` 并等待其健康检查通过，然后在容器网络内对运行中的 API
跑完整 pytest 套件（黑盒 HTTP），结束即退出。需显式启用 profile：

```bash
docker compose --profile verify run --rm verify
```

### 本地直接运行

需要 Python 3.12（或兼容版本）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

本地跑测试（进程内 ASGI，无需启动服务）：

```bash
pytest
```

## 项目结构

```
app/
  models.py      # Pydantic 模型与单条采样取值校验
  parsing.py     # 原始 JSON 解析：按字面量校验数字写法（两位小数 / 禁科学计数法）
  analysis.py    # 重排、整批校验、区段识别 / 合并 / 峰值选择、累计超限量（定点运算）
  main.py        # FastAPI 路由、严格 JSON 路由类与 422 异常处理
tests/
  test_api.py    # 阈值、持续时长、合并 / 分开、乱序、拒绝边界、数字词法等用例
docker-compose.yml  # 仅 api 常驻；verify 复用其镜像做一次性验收（profile）
Dockerfile
```
