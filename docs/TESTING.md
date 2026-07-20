# 验收与韧性测试

## 安全边界

验证脚本默认拒绝执行，必须显式设置 `DOLA_TEST_ENV=test`。目标地址默认只允许 `localhost`、环回地址或本机主机名；测试专网远程地址还需传入 `--allow-remote`。请勿对生产环境设置该变量或使用远程开关。

故障注入脚本仅操作 Compose 中的 `redis`、`postgres`、`worker`，不停止 API，不删除容器，不删除卷，不执行 `docker compose down -v`。异常退出时脚本会尝试重新启动被停止的服务，但测试结束后仍应执行 `docker compose ps` 确认四项服务健康。

## 前置条件

- Python 3.11+，从仓库根目录执行命令。
- Docker Engine 与 Compose v2 可用。
- 使用独立测试数据卷和测试凭据，不复用生产卷。
- Compose 四服务已构建，API 映射至本机 `8088`。
- `reports/` 中 JSON 和运行报告默认不纳入版本控制。

## Linux 一键验收

一键入口覆盖四服务启动、当前 JSON 只读副本→PostgreSQL→JSON 迁移回滚、管理员与客户网页/API 全程并行探活、30 并发及四容器资源峰值、API/Redis/PostgreSQL/Worker 故障恢复、全量备份恢复验证，以及 IP 白名单 HTTPS 配置生成。

```bash
cp .env.acceptance.example .env
chmod 600 .env
chmod +x scripts/accept-linux.sh
./scripts/accept-linux.sh
```

执行前必须完成以下配置：

- 将 `POSTGRES_PASSWORD` 和 `DOLA_ADMIN_PASSWORD` 替换为非默认随机密码。
- 保持 `COMPOSE_PROJECT_NAME` 以 `dola-acceptance` 开头，确保使用独立容器和数据卷。
- 将 `DOLA_HTTPS_DOMAIN`、证书、私钥和 `DOLA_IP_ALLOWLIST` 改为实际验收值。
- 证书和私钥必须已存在；脚本只生成 Nginx 准备配置，不修改系统 Nginx，也不开放防火墙。

成功时输出 `reports/linux-acceptance-时间戳.json` 与 Markdown 摘要，HTTPS 配置写入 `reports/nginx-dola-https.conf`，备份写入 `backups/acceptance-时间戳/`。任一阶段失败均返回非零退出码并保留已收集证据。

安全门禁同时要求 Linux、`DOLA_ACCEPTANCE_ENV=acceptance`、`DOLA_TEST_ENV=test`、独立 Compose 项目和强密码。远程目标必须额外传 `--allow-remote`；脚本不会执行 `down -v`，恢复验证使用临时数据库，不覆盖当前业务库。

从 30 并发开始直至故障、迁移和备份结束，脚本在后台并行探测 `/admin`、`/client`、`/health/live` 和鉴权 `/health`。API 故障注入窗口内的中断单独记为预期失败，其余窗口任一目标失败都会令整体验收失败。迁移从当前 Compose 数据卷复制 JSON 到临时只读快照，只操作工作副本和临时数据库，并核对迁移前、写入 PostgreSQL、回滚后的任务数与文档数；回滚文件数、字节数和 SHA-256 摘要也必须与只读快照一致。

可选执行真实 Dola 任务，分档仅允许 `1`、`3`、`5`，按参数顺序逐档执行；任一任务失败或超时立即停止后续档位、写入报告并返回非零状态。该选项会真实消耗验收账号额度，默认关闭：

```bash
./scripts/accept-linux.sh --real-dola-stages 1,3,5 --real-dola-timeout 900
```

单独执行备份恢复或 HTTPS 准备：

```bash
export DOLA_TEST_ENV=test
python -m scripts.backup_restore --compose-file compose.yaml
python -m scripts.prepare_https --domain acceptance.example.com --certificate /etc/ssl/dola/fullchain.pem --private-key /etc/ssl/dola/privkey.pem --allow-cidr 192.0.2.10/32 --output reports/nginx-dola-https.conf
```

PowerShell：

```powershell
$env:DOLA_TEST_ENV = "test"
docker compose up -d --build
docker compose ps
```

Bash：

```bash
export DOLA_TEST_ENV=test
docker compose up -d --build
docker compose ps
```

## 阶梯并发

默认目标是无鉴权、无业务写入的 `/health/live`，依次执行 `1,5,10,20` 并发，每阶 15 秒，固定随机种子为 `20260718`。同一阶段每个虚拟用户的请求标识由种子、并发、用户编号和序号确定，可重现请求序列。

```bash
python -m scripts.ladder_concurrency --compose-file compose.yaml --stages 1,5,10,20 --duration 15 --cooldown 3 --seed 20260718
```

脚本输出：

- 每阶请求量、成功数、错误数和状态码分布。
- 吞吐量及最小、P50、P95、P99、最大延迟。
- Compose 所有容器的 CPU、内存、PID 峰值。
- `reports/ladder-时间戳.json` 原始数据和 `reports/ladder-时间戳.md` 摘要。

测试鉴权只读健康接口时传入 `--path /health --token TEST_TOKEN`。除非准备了可丢弃的数据和专用账号，不要将路径改为任务创建接口。

## 故障注入

```bash
python -m scripts.docker_fault_injection --compose-file compose.yaml --services redis,postgres,worker --recovery-timeout 120
```

每项故障按固定顺序串行执行：记录初始状态、立即停止单个服务、确认停止、采集 API 表现、重新启动、等待健康、执行依赖原生探针、确认 API 恢复。Redis 使用 `redis-cli ping`，PostgreSQL 使用 `pg_isready`，Worker 使用 Compose 健康检查。

如需让 API 健康响应同时反映 Redis 队列状态，可传入测试管理员 Token：

```bash
python -m scripts.docker_fault_injection --token TEST_ADMIN_TOKEN
```

## 验收标准

- 阶梯并发每阶至少产生一个请求，默认只读探针无非 2xx/3xx 响应。
- 每个故障均观察到服务停止，且在超时内恢复为健康。
- Redis 与 PostgreSQL 原生探针成功，Worker 健康检查成功。
- 每次恢复后 API 返回 HTTP 200。
- JSON 报告 `passed` 为 `true`，Markdown 表格所有结论均为“通过”。
- 四个服务最终均为 `running` 且健康检查为 `healthy`。
- 管理网页、客户网页、公开存活 API 和鉴权健康 API 全程并行探活零非预期失败。
- 30 并发请求零错误，且采集到容器 CPU、内存和 PID 峰值。
- 当前 JSON 只读副本的迁移数量摘要与回滚文件摘要完全一致，备份校验和、RDB 校验及临时数据库恢复成功。
- 启用真实 Dola 分档时，每档全部任务必须返回成功代码和非空视频 URL，失败即报告。
- HTTPS 配置仅反代环回地址，启用 TLS 1.2/1.3 且拒绝白名单外 IP。

## 自动化测试

```bash
python -m unittest tests.test_validation_scripts -v
python -m unittest discover -s tests -v
docker compose config --quiet
```

单元测试不启动或停止容器，覆盖安全门禁、资源单位解析、分位数、峰值汇总和报告渲染。故障注入属于显式测试环境集成测试，不应放入默认 CI 单元测试阶段。
