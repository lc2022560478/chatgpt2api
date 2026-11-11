# 脱敏发布说明

本次于 2026-11-11（Asia/Shanghai）将当前开发版本的源码更新到
[lc2022560478/chatgpt2api](https://github.com/lc2022560478/chatgpt2api) 的 `main` 分支。
保留该仓库已有公开历史，不导入本地开发仓库的提交历史。原作者 MIT 版权声明保留。

## 包含的内容

- 当前 Python 后端、Next.js 前端、测试、文档与 Docker 部署源码，含本地功能修改。
- 无凭据的 `config.example.json`、`.env.example` 和防止误传的忽略规则。
- 本说明、检查报告与 SHA-256 文件清单。

## 排除或清空的内容

- 实际 `config.json`、后台密钥、账号与 OAuth 令牌、私有连接凭据。
- `data/` 下的账号库、数据库、备份、邮箱清单、日志、图片与任务记录。
- `.codex/`、本地操作记录、临时补丁与调试脚本、SDK 调试转储。
- `.env`、私钥、依赖目录、虚拟环境、缓存、编译产物与嵌套旧源码目录。
- 示例中清空代理、Cloudflare clearance、AI 审核、备份与 WebDAV 的实际配置和凭据。

运行目录及原始配置没有被修改。Docker 构建只复制 `config.example.json`，
实际 `config.json` 同时被 Git 和 Docker 构建忽略。

## 首次部署

1. 复制 `config.example.json` 为 `config.json`。
2. 设置自己的强随机 `CHATGPT2API_AUTH_KEY`，或填写本地 `config.json` 的 `auth-key`。
3. 导入自己的账号，并按需配置代理、备份与图片存储。
4. 执行 `docker compose up -d --build`，构建包含本仓库修改的镜像。

`.env` 仅由 Docker Compose 自动读取；直接启动 Python 时，请将密钥设置为进程环境变量或填写本地配置。

## 检查结果

- 与本地已知凭据和真实邮箱交叉比对：未发现残留。
- Gitleaks 8.30.1：通过。两处初始命中均为公开的 Auth0 SDK 名称/版本 header；
  已对照上游公开提交 `e55aef2` 核实，配置仅对该固定 header 和两个源文件作精确例外。
- Python 3.13 语法检查、JSON 解析、三个 Compose 配置检查和 solver JavaScript 语法检查通过。
- 前端 TypeScript 5.9.3：75 个项目文件，0 项诊断，不输出构建产物。
- 无网络的临时容器回归：277 项，267 项通过，10 项 WARP 专用环境测试跳过，0 失败、0 错误。
- 仅选取使用模拟依赖的模型单元测试；连接真实服务的集成用例未在离线检查中执行。

回归中发现并修复了图片并发全部失败时 `next()` 误传三个参数的问题，保留模型不可用错误与超时错误的优先级。
该最小修复仅应用于本次发布副本，现有回归用例已覆盖。

检查范围为本次发布的源码和新增提交，不代表重新审计仓库原有公开历史。
扫描可降低泄露风险，无法覆盖所有未知的敏感信息格式。机器检查详情见
[SANITIZATION_REPORT.json](SANITIZATION_REPORT.json)，文件校验见 [MANIFEST.sha256](MANIFEST.sha256)。
