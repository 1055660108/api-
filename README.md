# dola_fetch_service

这是一个基于 FastAPI + Playwright 的任务服务，主要用于利用Dola的AI创作功能，实现无限视频生成，带视频去水印，带 Web 管理面板、任务队列、代理提取 API 配置、并发控制和视频结果查询能力。

## 一键安装

在 Linux 服务器上使用 `root` 执行。脚本会自动检测依赖，缺少依赖会自动安装；安装源失败时会自动尝试切换常用镜像源；安装成功后会启动服务，并输出面板地址和 API Token。

支持系统：

- Debian / Ubuntu
- CentOS / RHEL / Rocky / AlmaLinux

一键安装：

```bash
bash <(curl -fsSL "https://raw.githubusercontent.com/1055660108/api-/main/scripts/install.sh")
```

安装完成后窗口提示：

```text
安装成功
面板地址：http://服务器IP:8088/admin
API Token：xxxxxxxx
```

查看 API Token：

```bash
/opt/dola-fetch-service/scripts/show-token.sh
```

重置 API Token：

```bash
/opt/dola-fetch-service/scripts/reset-token.sh
```

查看服务状态：

```bash
systemctl status dola-fetch-service
systemctl status dola-fetch-service-worker
```

重启服务：

```bash
systemctl restart dola-fetch-service dola-fetch-service-worker
```

## 功能

- Web 管理面板：`/admin`
- API Token 登录和接口鉴权
- 支持文本任务和带图任务
- 支持任务列表、查询、删除、清空
- 支持任务视频缓存七天自动清理，避免任务文件和图片长期堆积
- 支持修改并发数量
- 支持修改代理提取 API
- Redis 可靠队列、延迟重试和 Worker 崩溃租约恢复
- API 与浏览器 Worker 独立进程部署
- 平台级令牌桶限流、熔断恢复和队列过载保护
- 根据容器或主机内存压力自适应收缩 Worker
- Docker Compose 一键启动 API、Worker、Redis、PostgreSQL 四服务
- 每个任务使用独立的浏览会话数据
- 任务完成后自动关闭会话并回收内存

## 队列与 Worker

生产环境使用 Redis 队列，API 仅创建任务，不再内嵌浏览器 Worker。独立 Worker 入口为：

```bash
python worker.py
```

环境变量配置：

```bash
export DOLA_QUEUE_BACKEND=redis
export DOLA_REDIS_URL=redis://127.0.0.1:6379/0
export DOLA_QUEUE_NAMESPACE=dola:tasks
export DOLA_QUEUE_VISIBILITY_TIMEOUT=180
```

Redis 队列将任务原子移动到 processing，并通过租约心跳确认 Worker 存活；租约过期时，未提交到上游平台的任务恢复为 pending，已提交任务恢复为 submitted 继续查询结果。延迟重试保存在有序集合，到期后自动回到 ready 队列。

未配置 `DOLA_QUEUE_BACKEND` 时默认使用 `file`，继续通过任务文件扫描领取，便于本地运行和兼容既有测试。生产 Redis 模式要求 API 与 Worker 使用共享任务存储；多机部署时应同时配置 `DOLA_DATABASE_URL`。

## Docker Compose 四服务部署

复制环境变量模板，至少修改 PostgreSQL 与管理员密码后启动：

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

Compose 启动四个服务：`api` 提供网页和 API，`worker` 执行 Playwright 任务，`redis` 保存可靠队列与平台韧性状态，`postgres` 保存业务数据。浏览器上传文件与运行配置保存在共享 `app-data` 卷，Redis 和 PostgreSQL 分别使用独立持久卷。

`api` 和 `worker` 必须挂载同一个 `app-data` 卷：前者写入上传图片和配置，后者读取图片并持续写入 Worker 健康状态。API 健康检查请求无需鉴权的 `GET /health/live`；Worker 健康检查验证管理协程、看门狗和执行协程均存活，`docker compose ps` 应显示两个应用服务均为 `healthy`。

管理面板地址保持不变：

```text
http://127.0.0.1:8088/admin
```

常用运维命令：

```bash
docker compose logs -f api worker
docker compose restart api worker
docker compose exec api python scripts/storage_migrate.py --help
docker compose down
docker compose down -v
```

`docker compose down -v` 会删除全部持久化数据，只应在确认清空环境时执行。生产环境必须修改 `.env` 中的 `POSTGRES_PASSWORD` 和 `DOLA_ADMIN_PASSWORD`，不得使用模板默认值。

## 平台韧性与降载

API 在创建任务前检查 Redis 队列状态与积压深度。队列不可用或达到高水位时返回 `503` 和 `Retry-After`，普通 API 保持 FastAPI 的 `{"detail":"..."}` 结构，OpenAI 接口保持 `{"error":{...}}` 结构，既有网页和客户端无需修改解析逻辑。

Worker 对 `dola`、`doubao`、`qianwen` 分别执行共享令牌桶限流。连续可重试且非账号故障达到阈值后打开平台熔断器，恢复窗口结束后只允许一个半开探测任务；探测成功关闭熔断器，失败重新进入恢复窗口。Redis 模式下状态在所有 Worker 实例间共享，文件队列模式使用进程内状态以兼容本地开发。

Worker 每五秒读取 cgroup v1/v2 或主机内存使用率，并动态计算有效并发。达到高水位时逐步收缩，达到临界水位时保留最低 Worker 数，资源恢复后自动回升。`GET /health` 原字段保持不变，并在 `components.resources` 与 `components.platforms` 增量返回资源和熔断状态。

| 环境变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `DOLA_PLATFORM_RATE_PER_MINUTE` | `30` | 每个平台每分钟补充令牌数 |
| `DOLA_PLATFORM_BURST` | `5` | 每个平台令牌桶容量 |
| `DOLA_CIRCUIT_FAILURE_THRESHOLD` | `5` | 打开熔断器的连续失败数 |
| `DOLA_CIRCUIT_RECOVERY_SECONDS` | `60` | 熔断恢复及半开探测窗口 |
| `DOLA_QUEUE_HIGH_WATERMARK` | `1000` | 拒绝新任务的队列总深度 |
| `DOLA_MEMORY_HIGH_RATIO` | `0.80` | 开始动态收缩的内存比例 |
| `DOLA_MEMORY_CRITICAL_RATIO` | `0.92` | 收缩至最低并发的内存比例 |
| `DOLA_MINIMUM_WORKERS` | `1` | 降载期间保留的最低 Worker 数 |

限流和熔断只延迟已入队任务，不改变任务 ID、状态字段或轮询端点。队列过载拒绝发生在扣除额度和创建任务之前，因此不会产生孤儿任务或额度退款竞态。

## 环境要求

- Python 3.11+
- Linux 服务器，推荐 Debian / Ubuntu
- Playwright 支持的 Chromium 运行依赖

## 本地运行

Linux / macOS：

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python run.py
```

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe run.py
```

本地打开：

```text
http://127.0.0.1:8088/admin
```

运行自动化测试：

```bash
python -m unittest discover -s tests -v
```

验证 Compose 配置：

```bash
docker compose config --quiet
```

## 并发与故障注入验证

仓库提供可复现阶梯并发、Docker 资源峰值采集以及 Redis/PostgreSQL/Worker 故障注入脚本。所有脚本默认仅允许显式标记的本机测试环境，阶梯测试默认请求无业务写入的 `/health/live`，故障注入不会删除容器或数据卷。

```bash
export DOLA_TEST_ENV=test
python -m scripts.ladder_concurrency --compose-file compose.yaml --stages 1,5,10,20 --duration 15 --seed 20260718
python -m scripts.docker_fault_injection --compose-file compose.yaml --services redis,postgres,worker
```

结果写入 `reports/` 的 JSON 原始记录和 Markdown 摘要。安全边界、PowerShell 命令、指标定义、故障顺序和验收标准见 [测试文档](docs/TESTING.md)。

## Linux 一键验收

独立验收机可一次完成四服务、迁移回滚、网页/API 连续探活、30 并发资源、故障恢复、备份恢复及 IP 白名单 HTTPS 准备：

```bash
cp .env.acceptance.example .env
chmod 600 .env
chmod +x scripts/accept-linux.sh
./scripts/accept-linux.sh
```

运行前必须替换模板密码、域名、证书路径和白名单。安全门禁要求 Linux、独立的 `dola-acceptance*` Compose 项目和显式测试标记，拒绝默认密码及生产卷复用。详细产物、验收标准和单项执行命令见 [测试文档](docs/TESTING.md)。

## PostgreSQL 存储与停机迁移

设置 `DOLA_DATABASE_URL` 后，任务元数据、结果、账号、临时令牌、用户、套餐和运行时状态使用 PostgreSQL；任务上传图片仍保存在 `DOLA_DATA_DIR/tasks`。未设置该变量时保持原 JSON 文件存储，现有函数和 HTTP API 无需修改。

首次迁移必须同时停止 API 和 Worker，目标数据库必须为空：

```bash
systemctl stop dola-fetch-service dola-fetch-service-worker
export DOLA_DATABASE_URL='postgresql://dola:password@127.0.0.1:5432/dola'
cd /opt/dola-fetch-service
.venv/bin/python scripts/storage_migrate.py to-postgres
systemctl start dola-fetch-service dola-fetch-service-worker
```

迁移会在数据目录内创建 `.json-backup`；Compose 部署时该备份随 `app-data` 卷持久化。任一步失败会清空本次写入并删除不完整备份。回滚会以 PostgreSQL 当前数据重建 JSON，并从备份恢复任务图片：

```bash
systemctl stop dola-fetch-service dola-fetch-service-worker
export DOLA_DATABASE_URL='postgresql://dola:password@127.0.0.1:5432/dola'
cd /opt/dola-fetch-service
.venv/bin/python scripts/storage_migrate.py to-json
unset DOLA_DATABASE_URL
systemctl start dola-fetch-service dola-fetch-service-worker
```

Compose 部署执行迁移时使用同一镜像和共享卷：

```bash
docker compose stop api worker
docker compose run --rm api python scripts/storage_migrate.py to-postgres
docker compose up -d api worker
```

脚本默认同时检测 `DOLA_DATA_DIR/.service-running` 和 `.worker-health.json` 停机标记；确认残留标记对应的进程已停止时才可传 `--force`。数据库 schema 同时保存在 `schema/001_postgresql.sql`，应用启动时也会幂等初始化。

## 配置文件

服务器运行配置默认保存在：

```text
/var/lib/dola-fetch-service/config.json
```

可以参考 `config.example.json`：

```json
{
  "api_token": "首次启动自动生成",
  "browser_workers": 2,
  "proxy_api_url": "https://example.com/get-proxy?num=1&type=txt",
  "proxy_api_scheme": "http",
  "proxy_api_timeout_seconds": 20,
  "reclaim_memory_after_task": true,
  "drop_os_cache_when_idle": false
}
```

`proxy_api_url` 应返回单个 `ip:port`。服务会在每个任务启动前直接请求代理提取 API，然后只把这个代理传给 Chromium。

也可以用环境变量指定默认代理提取 API：

```bash
export DOLA_DEFAULT_PROXY_API_URL="https://example.com/get-proxy?num=1&type=txt"
```

首次初始化前通过环境变量指定单一管理员账号密码：

```bash
export DOLA_ADMIN_USERNAME="your-admin"
export DOLA_ADMIN_PASSWORD="your-strong-password"
```

密码仅以 PBKDF2-SHA256 哈希写入配置。管理面板使用服务端会话和 HttpOnly Cookie，原有 `X-API-Token` 与 `Authorization: Bearer` 调用保持兼容。未指定时初始账号密码为 `admin` / `admin123456`，登录后应立即在设置页修改密码。

## OpenAI 兼容接口

使用 `Authorization: Bearer <API_TOKEN>` 鉴权。

- `GET /v1/models` 返回当前已启用模型，模型 ID 格式为 `平台:模型`。
- `POST /v1/chat/completions` 接受非流式文本请求并异步创建视频任务。
- `stream=true`、`n != 1`、多模态消息和工具调用暂不支持。
- 返回的 `choices[0].message.content` 是 JSON 字符串，包含 `task_id` 和 `result_endpoint`。
- 使用相同 Token 请求 `GET /tasks/{task_id}` 轮询最终视频 URL。

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"qianwen:万相 2.7","messages":[{"role":"user","content":"一只猫在草地上行走"}]}'
```

## 常用接口

所有接口都需要在请求 Header 中携带：

```text
X-API-Token: <API Token>
```

### GET /health

检查服务状态，返回服务是否可用、并发配置和正在执行的任务 ID。

请求示例：

```bash
curl -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/health
```

响应示例：

```json
{"ok":true,"browser_workers":4,"active":[]}
```

### GET /config/proxy-api

读取当前代理提取 API 配置。

请求示例：

```bash
curl -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/config/proxy-api
```

响应示例：

```json
{"proxy_api_url":"https://example.com/get","proxy_api_scheme":"http","proxy_api_timeout_seconds":20}
```

### GET /config/workers

读取当前并发配置。

请求示例：

```bash
curl -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/config/workers
```

响应示例：

```json
{"browser_workers":4}
```

### POST /config/workers

修改并发数量，范围 1 - 100。

请求示例：

```bash
curl -X POST -H "X-API-Token: $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"browser_workers":4}' \
  http://SERVER_IP:8088/config/workers
```

响应示例：

```json
{"ok":true,"browser_workers":4}
```

### POST /config/proxy-api

修改代理提取 API。

请求示例：

```bash
curl -X POST -H "X-API-Token: $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"proxy_api_url":"https://example.com/get","proxy_api_scheme":"http"}' \
  http://SERVER_IP:8088/config/proxy-api
```

响应示例：

```json
{"ok":true,"proxy_api_url":"https://example.com/get","proxy_api_scheme":"http","proxy_api_timeout_seconds":20}
```

### POST /tasks

提交任务，使用 `multipart/form-data`，字段为 `prompt`、`ratio`、`images`，图片最多按后端配置接收。

请求示例：

```bash
curl -X POST -H "X-API-Token: $API_TOKEN" \
  -F "prompt=一个人在奔跑健身" \
  -F "ratio=9:16" \
  http://SERVER_IP:8088/tasks
```

响应示例：

```json
{"id":"0123456789abcdef0123456789abcdef"}
```

### GET /tasks

获取当前任务列表。

请求示例：

```bash
curl -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/tasks
```

响应示例：

```json
{"tasks":[{"id":"0123456789abcdef0123456789abcdef","prompt_preview":"一个人在奔跑健身","status":"success","image_count":0}]}
```

### DELETE /tasks

批量清空未在生成中的任务，生成中的任务会自动保留。

请求示例：

```bash
curl -X DELETE -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/tasks
```

响应示例：

```json
{"ok":true,"deleted":10,"skipped":[]}
```

### GET /tasks/{task_id}

查询单个任务状态和结果。

请求示例：

```bash
curl -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/tasks/0123456789abcdef0123456789abcdef
```

响应示例：

```json
{"code":"2","text":"","url":"https://example.com/video.mp4"}
```

### DELETE /tasks/{task_id}

删除未在生成中的任务；生成中任务会返回不可取消。

请求示例：

```bash
curl -X DELETE -H "X-API-Token: $API_TOKEN" http://SERVER_IP:8088/tasks/0123456789abcdef0123456789abcdef
```

响应示例：

```json
{"ok":true}
```

## 服务器各项命令

服务名：`dola-fetch-service`

查看服务状态：

```bash
systemctl --no-pager --full status dola-fetch-service
```

重启服务：

```bash
systemctl restart dola-fetch-service
```

停止服务：

```bash
systemctl stop dola-fetch-service
```

启动服务：

```bash
systemctl start dola-fetch-service
```

查看实时日志：

```bash
journalctl -u dola-fetch-service -f --no-pager
```

查看最近日志：

```bash
journalctl -u dola-fetch-service -n 200 --no-pager
```

查看配置文件：

```bash
cat /var/lib/dola-fetch-service/config.json
```

查看 Token：

```bash
/opt/dola-fetch-service/scripts/show-token.sh
```

重置 Token：

```bash
/opt/dola-fetch-service/scripts/reset-token.sh
```

清空任务数据：

```bash
find /var/lib/dola-fetch-service/tasks -mindepth 1 -maxdepth 1 -exec rm -rf {} +
```
