# Docker Compose 升级、备份与回滚

## 部署配置与数据

Compose 项目、服务和容器名均为 `llm-dashboard`，本地构建镜像为 `llm-dashboard:local`。数据目录 `/data` 使用命名卷，默认实际卷名为 `llm-dashboard-data`，可通过 `DATA_VOLUME_NAME` 指定；仓库目录名不影响卷名。

```text
/data/
├─ monitor.sqlite3
├─ monitor.sqlite3-wal / monitor.sqlite3-shm  # 运行时可能存在
├─ server.lock                             # 单进程锁文件
└─ artifacts/YYYY/MM/检测ID/
   ├─ result.svg
   └─ thumbnail.png
```

本文命令在仓库根目录执行。维护时使用与启动时相同的 Compose 文件：

| 部署方式 | 命令前缀 |
| --- | --- |
| 本地构建 | `docker compose` |
| GHCR 镜像 | `docker compose -f compose.yaml -f compose.ghcr.yaml` |
| GHCR 镜像并沿用外部卷 | `docker compose -f compose.yaml -f compose.ghcr.yaml -f compose.override.yaml` |

下文默认使用本地构建命令。使用 GHCR 的实例替换命令前缀，并按 [镜像部署指南](github-actions.md) 更新。指定 `-f` 后不会自动加载 `compose.override.yaml`，需要显式带上已有覆盖文件。

数据库含 API Key、密码哈希、系统设置和检测历史，备份应限制访问并存放在安全位置。完整备份同时保留数据库和 `artifacts/`；只备份数据库无法恢复文件形式的图形。不要提交数据、真实 `.env` 或备份。

## 沿用已有数据卷

升级前确认当前容器实际使用的数据卷：

```sh
docker inspect llm-dashboard --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

如果现有容器名称不同，使用它的实际名称查询。以上命令适用于命名卷；输出为空时应检查挂载类型和实际数据位置，不要继续创建空卷代替已有数据。

新配置需要沿用另一部署的已有命名卷时，先备份并停止原容器，再创建本机 `compose.override.yaml`。将 `name` 换成查到的真实卷名：

```yaml
volumes:
  llm-dashboard-data:
    external: true
    name: existing-data-volume
```

`external: true` 使用已有卷，并在卷不存在时直接报错；它不会改名、复制或删除原卷。已有卷属于另一 Compose 项目时，使用这份外部卷配置，避免仅修改 `DATA_VOLUME_NAME` 后出现卷标签不匹配。保留覆盖文件，并确保后续维护命令加载它。

不要让两个应用容器同时写入同一数据卷。升级和迁移期间不要执行 `docker compose down -v`，该命令会删除 Compose 管理的数据卷。

## 升级前的完整备份

先在后台暂停自动检测，等待执行中任务完成，再停止服务。记录当前镜像、卷名和卡片 / 模型数量；已有实例不需要重新初始化管理员密码。

以下步骤在拉取或构建新镜像之前执行，每一步成功后再继续：

```sh
umask 077
LLM_BACKUP_DIR="backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LLM_BACKUP_DIR"
LLM_BACKUP_IMAGE=$(docker inspect llm-dashboard --format '{{.Image}}')
LLM_DATA_VOLUME=$(docker inspect llm-dashboard --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
test -n "$LLM_DATA_VOLUME"
docker volume inspect "$LLM_DATA_VOLUME" --format '{{.Name}}'
docker image tag "$LLM_BACKUP_IMAGE" llm-dashboard:before-upgrade
docker compose stop llm-dashboard
docker run --rm --user 0 -v "$LLM_DATA_VOLUME:/data:ro" --entrypoint tar llm-dashboard:before-upgrade -C /data -czf - . > "$LLM_BACKUP_DIR/data.tar.gz"
docker image save llm-dashboard:before-upgrade -o "$LLM_BACKUP_DIR/image.tar"
tar -tzf "$LLM_BACKUP_DIR/data.tar.gz"
```

归档应包含 `monitor.sqlite3` 和已有的图形目录，并保留可能存在的 WAL / SHM。临时备份容器只执行归档，不启动应用。记录备份目录及对应源码版本；每次升级保留独立备份，避免覆盖唯一副本。

如果仅需在线备份数据库，可以使用 SQLite 备份 API：

```sh
umask 077
LLM_SNAPSHOT_DIR="backups/database-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LLM_SNAPSHOT_DIR"
docker compose exec -T llm-dashboard python -c "import sqlite3; src=sqlite3.connect('/data/monitor.sqlite3'); dst=sqlite3.connect('/data/database-snapshot.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker cp llm-dashboard:/data/database-snapshot.sqlite3 "$LLM_SNAPSHOT_DIR/monitor.sqlite3"
```

在线快照不包含图形文件。需要数据库与图形相互匹配的完整备份时，使用上面的停机归档流程；不要直接复制正在写入的主数据库文件。

## 更新与验收

备份完成后，本地构建实例执行：

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 llm-dashboard
```

GHCR 实例使用相应文件更新：

```sh
docker compose -f compose.yaml -f compose.ghcr.yaml pull llm-dashboard
docker compose -f compose.yaml -f compose.ghcr.yaml up -d --no-build
```

如有 `compose.override.yaml`，以上两条命令都追加该覆盖文件，位置在 `pull` / `up` 之前。首次启动可能迁移数据库，旧镜像不保证能直接读取升级后的数据；升级失败时按下一节同时恢复镜像和备份。

按 [部署验收指南](deployment-verification.md) 检查容器健康、原密码登录、数据卷、配置和历史。确认无误后恢复计划中的自动检测，观察首批任务及错误日志。需要 API 调用的检测会消耗上游额度，应使用预期的卡片和计划。

## 恢复到新卷与回滚

优先恢复到一个新卷，保留故障时的数据用于排查。回滚应使用同一次备份保存的镜像与完整数据归档。以下 `LLM_BACKUP_DIR` 设为待恢复备份的真实目录，不要将它指向空目录：

```sh
LLM_BACKUP_DIR=backups/待恢复备份目录
docker image load -i "$LLM_BACKUP_DIR/image.tar"
docker compose stop llm-dashboard
export LLM_RESTORE_VOLUME="llm-dashboard-restored-$(date -u +%Y%m%dT%H%M%SZ)"
docker volume create "$LLM_RESTORE_VOLUME"
docker run --rm --user 0 -i -v "$LLM_RESTORE_VOLUME:/data" --entrypoint tar llm-dashboard:before-upgrade -C /data -xzf - < "$LLM_BACKUP_DIR/data.tar.gz"
docker run --rm --user 0 -v "$LLM_RESTORE_VOLUME:/data" --entrypoint chown llm-dashboard:before-upgrade -R monitor:monitor /data
docker run --rm --user 10001 -v "$LLM_RESTORE_VOLUME:/data" --entrypoint python llm-dashboard:before-upgrade -c "import sqlite3; c=sqlite3.connect('file:/data/monitor.sqlite3?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

使用未存在的新卷名。当前镜像以 UID 10001 的 `monitor` 用户运行，用户组由镜像创建，恢复时通过 `monitor:monitor` 设置权限。恢复其他版本前确认对应镜像的运行用户。完整性检查应返回 `ok`，还需确认归档中已有图形文件已恢复。

在本机创建 `compose.restore.yaml`：

```yaml
services:
  llm-dashboard:
    image: llm-dashboard:before-upgrade
volumes:
  llm-dashboard-data:
    external: true
    name: ${LLM_RESTORE_VOLUME:?请设置恢复卷名}
```

然后启动恢复镜像和新卷：

```sh
docker compose -f compose.yaml -f compose.restore.yaml up -d --no-build --force-recreate
docker compose -f compose.yaml -f compose.restore.yaml ps
```

如果部署还依赖其他端口或环境覆盖文件，按原顺序带上它们，并让 `compose.restore.yaml` 位于最后，确保恢复镜像和卷生效。通过验收后继续使用相同文件管理服务；新终端需重新设置 `LLM_RESTORE_VOLUME` 为这次恢复的卷名。

不要仅切回旧镜像读取已升级的数据库。备份之后产生的新结果不在该备份内，回滚前另存故障时的数据；恢复后重新登录属于正常行为，会话不包含在备份中。

## 日常维护

登录后台，在“系统设置”中调整以下选项，无需编辑配置文件：

| 设置 | 默认值 | 范围 / 说明 |
| --- | --- | --- |
| 检测记录保留天数 | 90 天 | 1–3650；检测记录和请求指标 |
| 完整回答保留天数 | 30 天 | 1–3650；完整模型回答 |
| 图形保留天数 | 30 天 | 1–3650；图形和缩略图 |
| 检测并发 | 2 | 1–8；单人使用可设为 1 |

设置保存在 SQLite 中，保存后无需重启。降低并发不会打断现有请求；保留期限变更在下一轮每小时例行清理时执行。回答和图形的实际期限不超过检测记录期限，运行中任务不清理。SQLite 删除记录后文件大小不一定立即缩小。

Compose 默认限制为 1 CPU、1 GB 内存和 256 个进程，日志按大小轮转。定期检查磁盘空间、最近成功检测、任务队列、失败原因，并实际验证备份能恢复。提高并发前确认资源余量和上游限流。

首次打开 `/admin` 时在网页输入并确认 12–256 字符的管理密码。修改密码使用后台“修改管理密码”，需验证当前密码并确认新密码；成功后所有会话失效，使用新密码重新登录。

同一浏览器、同一主机地址下，管理员会话有效期最长两小时。服务重启、会话到期或退出后需要重新登录；切换 IP、localhost 或域名不会继承另一个主机的 Cookie。
