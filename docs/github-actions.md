# GitHub Actions 发布与 GHCR 部署

仓库使用 `.github/workflows/docker-publish.yml` 构建 Docker 镜像，按 GitHub 仓库名生成全小写的镜像地址。仓库更名后，目标镜像为 `ghcr.io/crazycherrys/llm-dashboard`。GHCR 是 GitHub Container Registry，域名是 `ghcr.io`。

## 首次启用需要的信息

- GitHub 仓库为 `CrazyCherrys/llm-dashboard`，需要有推送代码的权限；仓库更名需要仓库管理权限。
- 本机 Git 使用 SSH，公钥已添加到具有仓库写入权限的 GitHub 账号。
- 默认发布分支为 `main`，仓库需要启用 GitHub Actions 并允许工作流使用 `actions/*` 和 `docker/*`。
- 首次发布后选择镜像包的可见性：公开镜像可直接拉取；私有镜像需要部署者有包读取权限。
- 新实例首次启动需要 12–256 字符的管理密码；已有实例继续使用数据库中的密码和原数据卷。

镜像发布使用 Actions 自动生成的 `GITHUB_TOKEN`，工作流声明 `contents: read` 和 `packages: write`，无需添加个人令牌作为 Actions Secret，也不需要提供服务器 SSH 密钥。Git 的 SSH 私钥留在本机，GitHub 账号只添加公钥；私有镜像拉取令牌在自己的终端输入，不发送到聊天中。此流程构建并发布镜像，服务器不会自动重启更新。

## 仓库更名、SSH 认证与推送

如果 GitHub 上仍叫 `manxue-ai`，先打开 [仓库设置](https://github.com/CrazyCherrys/manxue-ai/settings)，在 General 中将 Repository name 改为 `llm-dashboard`。工作流使用实际仓库名生成镜像地址，因此先更名再推送，才能发布到预期的 `ghcr.io/crazycherrys/llm-dashboard`。

以下命令适用于 Bash（Linux / macOS 或 Windows Git Bash）。已有可用 SSH 密钥并已添加到 GitHub 时，跳过生成步骤。需要新密钥时执行：

```sh
ssh-keygen -t ed25519 -C 'llm-dashboard-github'
```

按交互提示选择密钥文件路径和口令。默认路径为 `~/.ssh/id_ed25519`；不要覆盖已有密钥，已有文件时选择另一个尚不存在的路径，例如 `~/.ssh/llm-dashboard_ed25519`。下面以默认路径为例，自定义路径时同步替换 `ssh-add` 和公钥文件路径：

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

复制完整的 `.pub` 公钥内容，打开 GitHub [Settings → SSH and GPG keys](https://github.com/settings/keys)，选择 New SSH key，Key type 选择 **Authentication Key**，粘贴并保存。随后测试认证：

```sh
ssh -T git@github.com
```

首次连接时核对 [GitHub 官方主机指纹](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) 后确认。看到 `Hi <用户名>! You've successfully authenticated, but GitHub does not provide shell access.` 表示认证成功；该命令返回退出码 `1` 是正常情况，检查输出中的账号是否拥有仓库写入权限。

在本地仓库根目录将远程地址统一为 SSH：

```sh
git remote set-url origin git@github.com:CrazyCherrys/llm-dashboard.git
git remote -v
```

新克隆仓库使用 `git clone git@github.com:CrazyCherrys/llm-dashboard.git`。

以下以本地 `main` 分支为例；先确认工作区文件列表和当前分支，提交这次改名及发布配置，再推送。若这次改动已整理为本地提交，跳过 `git add` / `git commit`，直接执行 `git push -u origin main`：

```sh
git branch --show-current
git status --short
git add --all
git commit -m "Rename to llm-dashboard and publish Docker images to GHCR"
git push -u origin main
```

若当前在功能分支，先推送该分支并创建合入 `main` 的拉取请求。PR 验证成功后合入 `main`，才发布正式镜像。提交前确认真实密码文件、数据目录和备份未在待提交列表中。

### SSH 的 22 端口受限时

先测试 GitHub 的 SSH 443 端口：

```sh
ssh -T -p 443 git@ssh.github.com
```

成功后，将以下配置合并到本机 `~/.ssh/config` 中的 `github.com` 配置段，保留已有的密钥路径等设置。此后继续使用相同的 `git@github.com:CrazyCherrys/llm-dashboard.git` 远程地址：

```text
Host github.com
    Hostname ssh.github.com
    Port 443
    User git
```

详见 [GitHub：通过 HTTPS 端口使用 SSH](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)。

在 [Actions 页面](https://github.com/CrazyCherrys/llm-dashboard/actions) 查看工作流。如果 Actions 被禁用，在 Settings → Actions → General 启用，并允许使用所需 Actions；组织策略也必须允许发布包。工作流已显式申请发布所需的令牌权限，通常无需修改仓库的默认工作流权限。

## 构建触发和镜像标签

工作流展示名称为 **Test and publish Docker image**。所有触发方式先执行 Python 3.13 后端测试、全部前端 JavaScript 语法检查与两套浏览器测试，通过后再构建双架构镜像。外部 Actions 均固定到具体提交 SHA。

| 操作 | 结果 |
| --- | --- |
| 推送到 `main` | 验证并构建，发布 `latest`、`main` 和 `sha-<12 位提交号>` 镜像标签 |
| 推送稳定版本标签，如 `v1.2.3` | 验证并构建，发布 `1.2.3`、`1.2`、`1` 和 `sha-<12 位提交号>` |
| 推送预发行标签，如 `v1.2.3-rc.1` | 验证并构建，只发布 `1.2.3-rc.1` 和 `sha-<12 位提交号>` |
| 创建或更新指向 `main` 的 PR | 验证和构建，不登录 GHCR、不发布镜像 |
| Actions 页面手动选择 `main` 运行 | 验证并重新发布 `latest`、`main` 和 `sha-<12 位提交号>` |
| 手动选择其他分支或标签运行 | 验证和构建，不发布镜像 |

版本发布使用 `v` 开头的语义化版本标签。版本标签不更新 `latest` / `main`，预发行也不更新稳定版的主版本 / 次版本标签。镜像支持 Linux `amd64` 和 `arm64`，Docker 根据部署机器架构选择对应镜像。

在 [Actions 页面](https://github.com/CrazyCherrys/llm-dashboard/actions) 选择 **Test and publish Docker image**，打开对应提交的运行记录查看状态；失败时点开失败的任务和步骤查看日志。构建成功后，在 [镜像包页面](https://github.com/users/CrazyCherrys/packages/container/package/llm-dashboard) 确认镜像标签和架构。

发布版本示例，在已验证的提交上创建标签并推送：

```sh
git tag v1.0.0
git push origin v1.0.0
```

部署时可选择固定版本或镜像摘要，避免每次更新都直接跟随 `latest`。正式标签以 Actions 输出和 GHCR 包页面为准。

## 公开或私有镜像

首次发布的 GHCR 包默认是私有的。希望部署者无需登录拉取时，在镜像包页面的 Package settings → Danger Zone → Change visibility 中改为 Public；仓库公开不会自动使新镜像包公开。

保持私有时，在部署机器上使用有该包访问权限的 GitHub 账号及 PAT classic，令牌至少包含 `read:packages`。账号还必须有包读取权限；组织使用 SSO 时按组织要求授权该令牌。Bash 中输入令牌并通过标准输入登录，不将它写入命令参数：

```bash
read -r -p "GitHub username: " GHCR_USERNAME
read -r -s -p "GHCR read token: " GHCR_READ_TOKEN
echo
printf '%s' "$GHCR_READ_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
unset GHCR_READ_TOKEN GHCR_USERNAME
```

这是部署机器读取私有镜像的凭据。Git SSH 密钥只用于代码获取与推送；Docker 登录 GHCR 使用上述令牌，Actions 发布继续使用 `GITHUB_TOKEN`。

## 新实例从 GHCR 启动

先确认首次 Actions 发布已成功，镜像已经公开或已完成私有包登录。在仓库根目录执行；Windows 用户可按 README 的 PowerShell 方式设置 `ADMIN_TOKEN`，使用相同的 Compose 文件参数：

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

首页为 <http://127.0.0.1:18765/>，后台为 <http://127.0.0.1:18765/admin>。确认服务健康且可以登录后，执行下面的命令，从容器配置中移除首次初始化密码；数据库中的密码保留：

```sh
docker compose -f compose.yaml -f compose.ghcr.yaml up -d --no-build --force-recreate
```

`--no-build` 避免触发本地构建，镜像来自刚才的 `pull`。新实例默认使用 `llm-dashboard-data` 数据卷。

## 已迁移实例更新镜像

改名前的实例先完成 [项目改名与原数据卷迁移](operations.md#项目改名与原数据卷迁移)。保留引用原数据卷的本机 `compose.override.yaml`，升级前按操作指南备份。

**显式使用 `-f` 时，Compose 不会自动加载 `compose.override.yaml`。** 已有迁移实例的所有维护命令必须带上该文件，避免容器挂载新建空卷：

```sh
docker compose -f compose.yaml -f compose.ghcr.yaml -f compose.override.yaml pull llm-dashboard
docker compose -f compose.yaml -f compose.ghcr.yaml -f compose.override.yaml up -d --no-build
docker compose -f compose.yaml -f compose.ghcr.yaml -f compose.override.yaml ps
docker compose -f compose.yaml -f compose.ghcr.yaml -f compose.override.yaml logs --tail=100 llm-dashboard
docker inspect llm-dashboard --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

检查最后输出仍是原数据卷，使用原密码登录并核对模型、历史和图形。已有数据库无需再次设置 `ADMIN_TOKEN`。更新镜像不删除数据卷；不要使用 `down -v` 做升级。

新建实例后续更新使用上一节的两份文件；有本机卷覆盖配置的实例始终使用本节的三份文件。要固定版本，可在执行 `pull` / `up` 前设置：

```sh
export LLM_DASHBOARD_IMAGE=ghcr.io/crazycherrys/llm-dashboard:1.0.0
```

也可以在本机 `.env` 中设置 `LLM_DASHBOARD_IMAGE`。需先确认该标签已经发布。回滚时恢复版本匹配的镜像和数据备份，详见 [迁移、备份与回滚指南](operations.md)。

## 完成判定与官方参考

本地配置文件存在只说明发布流程已配置。首次远程发布完成需要同时满足：代码已推送、Actions 全部步骤成功、GHCR 中存在对应标签、部署机器能够拉取。未运行成功前不应将镜像视为已发布。

- [GitHub：发布 Docker 镜像](https://docs.github.com/en/actions/use-cases-and-examples/publishing-packages/publishing-docker-images)
- [GitHub：使用 Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [GitHub：管理包的访问权限与可见性](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [GitHub：工作流中设置 GITHUB_TOKEN 权限](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)
- [GitHub：生成 SSH 密钥并添加到 ssh-agent](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)
- [GitHub：测试 SSH 连接](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection)
