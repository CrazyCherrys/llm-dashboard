# LLM Dashboard

## 项目介绍

一个自托管的模型接口监控面板，支持 OpenAI 兼容的 Responses 和 Chat Completions 接口。通过定时逻辑题和 SVG 图形检测，查看模型接口的可用性、响应速度与检测结果。

## 功能介绍

- **模型管理**：按卡片和分类管理多个模型，自定义 API 地址、密钥和请求参数。
- **自动检测**：定时或手动运行逻辑题、图形检测，支持自定义提示词模板。
- **监控面板**：展示请求成功率、逻辑通过率、缓存率、响应速度和历史记录。
- **图形预览**：查看模型生成的 SVG、动画及视觉审核结果。
- **网页设置**：在后台创建或修改管理密码，调整检测并发和数据保留天数。
- **数据保存**：配置、历史记录和图形自动保存到 Docker 数据卷。

## 启动方法

安装 Docker 和 Docker Compose，然后执行：

```sh
git clone https://github.com/CrazyCherrys/llm-dashboard.git
cd llm-dashboard
docker compose up -d --build
```

启动后访问：

- 首页：<http://localhost:18765/>
- 管理后台：<http://localhost:18765/admin>

部署在服务器上时，将 `localhost` 替换为服务器 IP。

首次进入管理后台，设置至少 12 位的管理密码；登录后添加 API 地址、密钥和模型，再开启自动检测。密码、检测并发和数据保留时间都可在“系统设置”中修改，无需编辑配置文件。
