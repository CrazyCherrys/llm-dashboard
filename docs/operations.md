# LLM Dashboard 迁移、备份与回滚指南

## 数据目录和升级前检查

Compose 项目名、服务名、容器名均为 `llm-dashboard`，本地构建镜像为 `llm-dashboard:local`。默认数据目录 `/data` 挂载 Compose 命名卷，卷配置键与默认实际名称均为 `llm-dashboard-data`；实际名称可通过 `DATA_VOLUME_NAME` 设置，仓库目录名不影响卷名。数据目录包含：

```text
/data/
├─ monitor.sqlite3
├─ monitor.sqlite3-wal / monitor.sqlite3-shm  # 运行时可能存在
├─ server.lock                             # 单进程锁文件
└─ artifacts/YYYY/MM/检测ID/
   ├─ result.svg
   └─ thumbnail.png
```

通过 GHCR 镜像部署的实例，本文中的常规 Compose 维护命令应使用 `docker compose -f compose.yaml -f compose.ghcr.yaml`；沿用旧外部卷的实例还需追加 `-f compose.override.yaml`。具体命令见 [GHCR 部署指南](github-actions.md#已迁移实例更新镜像)，保持与启动服务时相同的镜像和卷配置。

数据库包含真实 API Key、密码哈希及历史回答，备份应按秘密文件保护，不提交 Git。备份数据库同时保留图形目录；只备数据库可以恢复配置和数值记录，但无法恢复新版本以文件保存的图形。

升级前记录当前镜像、容器状态和持久化卷，确认当前渠道 / 模型数量、自动检测是否开启。操作在仓库根目录执行：

```sh
docker compose ps
docker inspect llm-dashboard --format '{{.Image}}'
docker inspect llm-dashboard --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
LLM_UPGRADE_IMAGE=$(docker inspect llm-dashboard --format '{{.Image}}')
docker image tag "$LLM_UPGRADE_IMAGE" llm-dashboard:before-upgrade
```

这里仅输出镜像和卷标识，不运行完整的 Compose 环境配置输出，避免在终端打印初始化密码等内容。

## 项目改名与原数据卷迁移

下面是从改名前 `manxue-ai` 部署迁移到新名称的操作步骤，不代表现有正式服务已经完成切换。旧容器与数据卷的名称仅用于找到已有数据；新建实例直接按 README 部署。

先在旧后台暂停自动检测，等待所有执行中任务结束。更名后的 Compose 使用新服务名，不能用它的 `stop` 命令停止旧服务；直接按旧容器名称停止。以下 Bash 命令保存旧镜像和完整数据目录，原卷不会被删除：

```bash
umask 077
mkdir -p backups
LLM_RENAME_IMAGE=$(docker inspect manxue-ai --format '{{.Image}}')
LLM_RENAME_VOLUME=$(docker inspect manxue-ai --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
test -n "$LLM_RENAME_VOLUME"
docker volume inspect "$LLM_RENAME_VOLUME" --format '{{.Name}}'
docker image tag "$LLM_RENAME_IMAGE" llm-dashboard:before-rename
docker stop -t 30 manxue-ai
docker run --rm --user 0 -v "$LLM_RENAME_VOLUME:/data:ro" --entrypoint tar llm-dashboard:before-rename -C /data -czf - . > backups/data-before-rename.tar.gz
docker image save llm-dashboard:before-rename -o backups/image-before-rename.tar
tar -tzf backups/data-before-rename.tar.gz
```

每步成功后再继续，确认归档包含数据库与已有图形目录。没有成功识别旧卷时停止迁移并检查旧容器挂载，不要以新建空卷代替。备份文件含私密配置，不提交 Git。

在新配置所在的仓库根目录创建本机 `compose.override.yaml`，引用刚才确认的实际旧卷名称。原部署卷名通常为 `manxue-ai_manxue-ai-data`：

```yaml
volumes:
  llm-dashboard-data:
    external: true
    name: manxue-ai_manxue-ai-data
```

若实际卷名不同，将 `name` 改为刚才记录的名称。`external: true` 让 Compose 使用已有卷，不尝试管理它的旧项目标签，并在卷不存在时报错；不能只用 `DATA_VOLUME_NAME` 替代这份外部卷配置。普通 `docker compose` 命令会自动加载此覆盖文件，之后维护时保留它。

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 llm-dashboard
docker inspect llm-dashboard --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
curl --fail http://127.0.0.1:18765/api/auth/status
curl --fail http://127.0.0.1:18765/api/dashboard
```

确认新容器健康、挂载仍指向原卷，使用原密码登录并核对模型配置、历史与图形，再恢复原计划中的自动检测。会话 Cookie 改为 `llm_dashboard_admin_session`，切换后需重新登录。此方案保留原卷的物理名称；今后新建实例和恢复卷使用 `llm-dashboard` 命名。

旧容器保持停止状态供排查，不要再次启动它与新容器同时写同一卷。全程不要执行 `docker compose down -v`。若回滚，先停止新容器，再按 [恢复到新卷](#恢复到新卷避免覆盖现有数据) 的方法恢复 `llm-dashboard:before-rename` 与 `backups/data-before-rename.tar.gz`，保留原卷及备份；不要只换回旧镜像读取已升级的数据库。

## 推荐的完整停机备份

先在后台暂停自动检测，等待现有任务完成，再停止容器。后台暂停会取消未发送任务，已发送任务继续收尾。正常升级只需短暂停机，无需删除数据卷。

```sh
umask 077
mkdir -p backups
docker compose stop llm-dashboard
docker compose run --rm --no-deps --user 0 -T llm-dashboard tar -C /data -czf - . > backups/data-before-upgrade.tar.gz
docker image save llm-dashboard:before-upgrade -o backups/image-before-upgrade.tar
tar -tzf backups/data-before-upgrade.tar.gz
```

备份中应包含数据库和已有的 `artifacts/`。停止服务后将主数据库及可能存在的 WAL / SHM 一起归档，避免遗漏尚在 WAL 中的事务。临时备份容器只执行 tar，不启动调度器。

可另保存当前受 Git 管理的源码版本或工作区源码包，便于追溯镜像。不要把数据卷、真实 `.env` 或密码文件打进对外源码包。

## 在线数据库备份

仅需要备份数据库时，可使用 SQLite 备份 API获得一致快照：

```sh
docker compose exec -T llm-dashboard python -c "import sqlite3; src=sqlite3.connect('/data/monitor.sqlite3'); dst=sqlite3.connect('/data/database-snapshot.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker cp llm-dashboard:/data/database-snapshot.sqlite3 ./backups/database-snapshot.sqlite3
```

在线数据库快照不会自动复制图形文件。要获得数据库与产物匹配的完整备份，应暂停任务和清理，并使用停机归档流程。不要直接复制正在写入的主数据库文件，也不要通过 `down -v` 做备份或更新。

## 旧版数据库迁移

新服务首次读取数据时创建新表并执行一次版本迁移，`schema_migrations` 记录完成版本。迁移失败不应启动自动任务，先使用备份排查。

| 旧结构 | 新结构 |
| --- | --- |
| 一个节点 | 一个渠道与一个模型配置 |
| 节点名称 | 渠道初始卡片标题 |
| 节点地址与 Key | 渠道连接配置 |
| 全局间隔、重试与超时 | 复制到模型及其独立检测计划 |
| 双项检测历史 | 拆成关联同一旧记录的逻辑 / 图形检测轮次 |
| 管理员认证 | 保留原盐和密码哈希 |
| 内嵌 SVG | 兼容读取，仍经过安全校验 |
| 缺少缓存 / 首返指标 | 保持未知，不按 0 或总耗时补造 |
| 找不到原节点的历史 | 放入独立的暂停历史渠道，避免归到其他渠道 |

只有原全局自动检测已开启、原节点是活动节点且有密钥时，迁移后延续其自动检测。其他节点默认暂停；新建模型的检测项目也需要明确开启。这避免多节点在升级后突然全部产生调用。

迁移后的两类检测分别运行，不再把一个联合结果作为渠道请求样本。旧历史没有每次请求尝试明细，不能用于推算准确的请求成功率、缓存率和首次返回。

兼容迁移没有立即删除旧表，但旧 `runs` 中的回答 / `tests` / SVG 副本也按新保留期限清理，旧访客结果按原一小时期限清理；旧节点和设置表仍是私密配置。保留旧表不能代替独立回滚备份，应用启动后的迁移和清理会改变数据。

部署前用数据库副本进行迁移，并验证渠道数量、节点关联、密码哈希、旧图形、缺失指标和重复启动的幂等性。工作区测试包含独立旧格式夹具，并可在本地备份存在时验证真实部署数据库的迁移。

## 新版本部署与验收

以下命令适用于本地构建部署。使用 GHCR 镜像时，按 [GHCR 部署指南](github-actions.md#已迁移实例更新镜像) 执行 `pull` 后以 `up -d --no-build` 更新，并带齐镜像与卷覆盖文件。备份完成后，本地构建实例执行：

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 llm-dashboard
curl --fail http://127.0.0.1:18765/api/auth/status
curl --fail http://127.0.0.1:18765/api/dashboard
```

验收事项：

1. 容器健康，原密码可以登录；不要为了升级重新初始化密码。
2. 原节点按预期映射到渠道与模型，启停状态和旧历史保留。
3. 未填真实 API Key 的配置保持暂停，没有自动消耗。
4. 管理员可以新增、编辑、单项启停，并看到队列 / 执行状态。
5. 前台显示真实统计；无样本、无缓存字段、非流式首返分别有正确空状态。
6. 公开页面只读，访客入口返回 410，刷新大盘不产生请求。
7. 隐藏渠道在大盘、详情、历史、SVG 与缩略图均不可读。
8. 在同一局域网从 `http://服务器IP:18765/` 访问，而非访问浏览器自身的 `127.0.0.1`。
9. 恢复计划中的自动检测，观察首批任务及内存、线程、渲染进程和失败原因。

当前机器局域网地址为 <http://192.168.3.14:18765/>。如果网络或端口配置改变，以部署主机的实际地址为准。

后台登录使用最长两小时的浏览器会话 Cookie，在同一浏览器、同一主机地址下返回首页或刷新后重新进入后台会恢复登录。服务重启、会话到期或主动退出会要求重新登录，密码仍保留。切换 IP、localhost 或域名不会继承另一个主机的 Cookie，建议固定访问地址。会话只保存在进程内存中，不在备份和恢复时保留；恢复后重新登录属于正常行为。

## 恢复到新卷，避免覆盖现有数据

推荐从备份恢复到一个新的数据卷，验证后切换；保留故障时的数据卷用于排查。以下示例名称可按日期调整，所有命令都指向新卷：

```sh
docker compose stop llm-dashboard
docker volume create llm-dashboard-restored-20261011
docker run --rm --user 0 -i -v llm-dashboard-restored-20261011:/data --entrypoint tar llm-dashboard:before-upgrade -C /data -xzf - < backups/data-before-upgrade.tar.gz
docker run --rm --user 0 -v llm-dashboard-restored-20261011:/data --entrypoint chown llm-dashboard:before-upgrade -R 10001:10001 /data
docker run --rm --user 10001 -v llm-dashboard-restored-20261011:/data --entrypoint python llm-dashboard:before-upgrade -c "import sqlite3; c=sqlite3.connect('/data/monitor.sqlite3'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

本项目镜像用户 UID 为 10001。恢复其他镜像时先确认其运行用户。完整性检查应返回 `ok`；还要抽查图形文件存在以及备份对应的应用版本。

创建不包含密钥的 `compose.restore.yaml`：

```yaml
services:
  llm-dashboard:
    image: llm-dashboard:before-upgrade
volumes:
  llm-dashboard-data:
    external: true
    name: llm-dashboard-restored-20261011
```

启动旧镜像和恢复卷：

```sh
docker compose -f compose.yaml -f compose.restore.yaml up -d --no-build --force-recreate
docker compose -f compose.yaml -f compose.restore.yaml ps
```

之后的管理命令也使用相同的两份 Compose 文件，避免误切回原卷。要恢复到当前版本，可将覆盖文件中的镜像改为该版本经过验证的镜像。

如果本机旧镜像已丢失，先使用 `docker image load -i backups/image-before-upgrade.tar` 恢复。旧代码不保证能读取新结构，所以回滚要同时恢复版本匹配的数据库和产物，而不是只更换镜像。

备份之后产生的新结果不在旧备份中。回滚前另备份故障时的数据，不覆盖唯一副本；需要时再按检测记录关联人工导出。

## 保留策略与日常维护

默认检测记录和请求指标保留 90 天，完整回答 30 天，图形与缩略图 30 天；可分别用 `RUN_RETENTION_DAYS`、`OUTPUT_RETENTION_DAYS`、`ARTIFACT_RETENTION_DAYS` 配置，均至少为 1。

调度器每小时检查过期数据。产物和记录按批次删除，每次清理有时间预算，余下数据留到下次继续；不会仅删除 100 条后每天停止，避免长期积压。运行中任务不清理。删除文件失败保留引用，后续可再次清理。

回答和图形的实际期限取配置值与检测记录期限的较小值：例如记录保留 7 天、产物配置 30 天时，产物实际也最多保留 7 天，不让产物延长记录保留。删除记录前清理对应产物，不制造孤立文件。回答被清理后详情仍可显示请求统计及判定，但原始完整回答为空。SQLite 删除记录不一定马上缩小数据库文件，空页可供后续写入复用；不在正常调度期间频繁 VACUUM。

当前 Compose 默认 1 CPU、1 GB 内存、工作并发 2、渲染并发 1。先观察上游延迟和资源使用，再提高并发。提高并发不应突破上游限流，也不会提高 Chromium 渲染并发。

建议定期检查：备份能否恢复、磁盘可用空间、最近成功更新时间、队列长度、失败原因、数据库文件权限。容器日志按大小轮转，不启用请求正文或凭据日志。

## 常见问题

| 现象 | 检查方向 |
| --- | --- |
| 其他电脑打不开 | 使用服务器局域网 IP，检查端口映射和主机网络；`127.0.0.1` 只访问当前电脑 |
| 保存后不自动检测 | 渠道、模型和具体项目三层开关都必须开启，且配置有 Key |
| 首次返回为空 | 没有成功流式样本，或上游退回完整 JSON；总耗时仍可在详情查看 |
| 缓存率为空 | 上游没有返回有效缓存明细，不能通过猜测补齐 |
| 渠道成功但逻辑为红 | 接口完整返回，但答案不符合当前糖果题规则 |
| 图形存在但审核未完成 | 模型不支持图片、审核请求失败、渲染失败或整轮预算耗尽 |
| 下次时间到了仍等待 | 队列或工作线程正在处理其他任务，倒计时不保证精确发起时刻 |
| 重启后历史为灰 | 未完成请求标记中断，为避免重复计费没有自动重放 |
| 旧图或回答消失 | 达到对应保留期限，记录和指标可比产物保留更久 |
| 修改 ADMIN_TOKEN 不换密码 | 该变量只初始化首次密码，现有密码哈希优先 |
| 刷新后需重新登录 | 检查是否超过两小时、服务是否刚重启、是否切换了主机地址或浏览器拒绝 Cookie |

外网访问需要配置可达地址和 HTTPS 反向代理。无需为只读访客暴露管理员凭据；后台配置、任务执行与公开读取始终使用不同权限边界。
