# GitHub Actions 发布与 GHCR 部署

仓库通过 `.github/workflows/docker-publish.yml` 验证代码并构建 Docker 镜像，发布到 GitHub Container Registry（GHCR）。镜像地址为 `ghcr.io/<仓库所有者>/<仓库名>`，名称转换为全小写。`compose.ghcr.yaml` 默认使用 `ghcr.io/crazycherrys/llm-dashboard:latest`；使用其他仓库发布的镜像时，通过 `LLM_DASHBOARD_IMAGE` 指定地址。

## 发布配置

- 仓库需要启用 GitHub Actions，并允许工作流使用 `actions/*` 和 `docker/*`；组织策略也需要允许发布包。
- 发布任务使用自动生成的 `GITHUB_TOKEN`，权限为 `contents: read` 和 `packages: write`，无需配置额外的发布令牌。
- 工作流只构建并发布镜像。服务器更新由部署者执行后文的 Compose 命令。

## 构建触发与镜像标签

工作流名称为 **Test and publish Docker image**。各触发方式均先执行后端测试、前端 JavaScript 语法检查和浏览器测试，通过后构建 Linux `amd64` / `arm64` 双架构镜像。

| 操作 | 结果 |
| --- | --- |
| 推送到 `main` | 发布 `latest`、`main` 和 `sha-<12 位提交号>` |
| 推送稳定版本标签，如 `v1.2.3` | 发布 `1.2.3`、`1.2`、`1` 和 `sha-<12 位提交号>` |
| 推送预发行标签，如 `v1.2.3-rc.1` | 发布 `1.2.3-rc.1` 和 `sha-<12 位提交号>` |
| 创建或更新指向 `main` 的 PR | 验证代码和 Docker 构建，不发布镜像 |
| 在 Actions 页面手动选择 `main` 运行 | 发布 `latest`、`main` 和 `sha-<12 位提交号>` |
| 手动选择其他分支或标签运行 | 验证代码和 Docker 构建，不发布镜像 |

版本发布使用 `v` 开头的语义化版本标签。版本标签不更新 `latest` / `main`，预发行不更新主版本和次版本标签。Docker 根据部署机器架构选择对应镜像。

在已验证的提交上发布版本：

```sh
git tag v1.0.0
git push origin v1.0.0
```

在仓库的 Actions 页面查看运行结果，再在 GHCR 包页面确认对应标签和架构。只有发布任务成功且部署机器能够拉取，才能确认镜像可用。

## 公开与私有镜像

首次发布的 GHCR 包默认是私有的。公开仓库不会自动使新镜像包公开；需要公开拉取时，在包页面的 Package settings → Danger Zone → Change visibility 中改为 Public。

私有镜像需要部署机器登录 GHCR。使用有包读取权限的 GitHub 账号及至少包含 `read:packages` 的 PAT classic；组织启用 SSO 时，还需为该令牌授权。以下命令在 Bash 中交互输入凭据：

```bash
read -r -p "GitHub username: " GHCR_USERNAME
read -r -s -p "GHCR read token: " GHCR_READ_TOKEN
echo
printf '%s' "$GHCR_READ_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
unset GHCR_READ_TOKEN GHCR_USERNAME
```

该令牌用于部署机器拉取私有镜像，Actions 发布仍使用 `GITHUB_TOKEN`。

## 使用 Compose 启动

部署机器需要 Docker Engine 和 Docker Compose v2，并已获得 `compose.yaml`、`compose.ghcr.yaml`。在这两份文件所在目录执行命令；首次发布需要已成功，私有镜像需要已登录。

新实例首次启动需要 12–256 字符的管理密码。以下命令在 Bash 中设置初始化密码并启动容器：

```bash
docker compose -f compose.yaml -f compose.ghcr.yaml pull llm-dashboard
read -r -s -p "Initial admin password: " ADMIN_TOKEN
echo
export ADMIN_TOKEN
docker compose -f compose.yaml -f compose.ghcr.yaml up -d --no-build
unset ADMIN_TOKEN
docker compose -f compose.yaml -f compose.ghcr.yaml ps
docker compose -f compose.yaml -f compose.ghcr.yaml logs --tail=100 llm-dashboard
```

本机首页为 <http://127.0.0.1:18765/>，后台为 <http://127.0.0.1:18765/admin>；其他设备使用部署机器的 IP 地址访问。数据默认保存在 `llm-dashboard-data` 卷中。

`ADMIN_TOKEN` 仅用于首次初始化，不会覆盖已有数据库中的管理密码。确认服务健康且可以登录后，清除初始化密码环境变量并重建容器，将密码从容器配置中移除。如果在 `.env` 中设置了 `ADMIN_TOKEN`，需先删除该项：

```sh
unset ADMIN_TOKEN
docker compose -f compose.yaml -f compose.ghcr.yaml up -d --no-build --force-recreate
```

`--no-build` 确保使用已拉取的 GHCR 镜像，不触发本地构建。

## 更新与固定版本

更新前按 [Docker 运维指南](operations.md) 备份数据。已有实例无需再次设置 `ADMIN_TOKEN`：

```sh
docker compose -f compose.yaml -f compose.ghcr.yaml pull llm-dashboard
docker compose -f compose.yaml -f compose.ghcr.yaml up -d --no-build
docker compose -f compose.yaml -f compose.ghcr.yaml ps
docker compose -f compose.yaml -f compose.ghcr.yaml logs --tail=100 llm-dashboard
```

更新容器会保留数据卷，不要使用 `down -v` 做升级。使用自定义 Compose 覆盖文件时，各维护命令都需要显式追加相同的 `-f` 参数；指定 `-f` 后，Compose 不会自动加载 `compose.override.yaml`。

需要固定版本时，在拉取和启动前设置已发布的镜像标签：

```sh
export LLM_DASHBOARD_IMAGE=ghcr.io/crazycherrys/llm-dashboard:1.0.0
docker compose -f compose.yaml -f compose.ghcr.yaml pull llm-dashboard
docker compose -f compose.yaml -f compose.ghcr.yaml up -d --no-build
```

也可以将 `LLM_DASHBOARD_IMAGE` 写入部署目录的 `.env`，或使用 `ghcr.io/<仓库所有者>/<仓库名>@sha256:<摘要>` 固定镜像摘要。镜像地址和标签以实际发布结果为准。回滚时使用版本匹配的镜像和数据备份，详见 [Docker 运维指南](operations.md)。

## 官方参考

- [GitHub：发布 Docker 镜像](https://docs.github.com/en/actions/use-cases-and-examples/publishing-packages/publishing-docker-images)
- [GitHub：使用 Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [GitHub：管理包的访问权限与可见性](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [GitHub：工作流中设置 GITHUB_TOKEN 权限](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)
