# Agent Workbench

基于 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的自托管部署配置。仓库内容根据一套实际运行配置整理，提供可复用的 Docker Compose、环境变量和模型配置示例；不包含 Hermes 上游源码，也不包含服务器运行数据。

## 部署内容

- `compose.yaml`：启动 Hermes Gateway 和 Dashboard，容器镜像固定到当前部署使用的 manifest digest；Dashboard 仅映射到服务器 `127.0.0.1:9119`。
- `.env.example`：Cicadas API Key 和 Dashboard Basic Auth 凭据的变量名示例，不含真实值。
- `config.yaml.example`：Cicadas OpenAI-compatible API 的端点、传输类型和默认模型示例。

## 启动

要求已安装 Docker Engine 和 Docker Compose plugin。

```bash
mkdir -p data
cp .env.example .env
cp config.yaml.example data/config.yaml
```

编辑 `.env`，填入自己的 `CICADAS_API_KEY`、Dashboard 用户名和强密码，并为 `HERMES_DASHBOARD_BASIC_AUTH_SECRET` 设置独立随机值。确认 `data/config.yaml` 中的 API 地址和模型 ID 与你的服务商配置相符。不要把真实密钥写进 `config.yaml`；该文件通过 `key_env` 从环境变量读取密钥。

启动并检查：

```bash
docker compose up -d
docker compose ps
docker compose logs -f hermes
```

Dashboard 默认只在服务器本机可访问：`http://127.0.0.1:9119`。从电脑访问时，可在本地另开 SSH 隧道：

```bash
ssh -N -L 9119:127.0.0.1:9119 user@your-server
```

然后在本机浏览器打开 `http://127.0.0.1:9119`。不要把端口改成 `0.0.0.0:9119:9119` 直接暴露到公网。若要经域名公开访问，需要另外配置 TLS、反向代理和访问控制；本仓库没有配置这些部分。

## 数据与密钥

`.env` 含 API Key 和 Dashboard 凭据；`data/` 是持久化目录，运行后可能包含认证信息、会话、数据库、日志、记忆和缓存。两者都由 `.gitignore` 排除，不要提交到 GitHub；备份时也要按敏感数据保管。

## 项目边界与升级

本仓库维护部署模板和集成说明，不是 Hermes Agent 的源码分支。镜像按 digest 固定，以保持与当前部署配置一致；升级时应有意选择并验证新的上游镜像 digest，再更新 `compose.yaml`。更新前备份 `data/`。

## 参考

- [Hermes Agent 源码](https://github.com/NousResearch/hermes-agent)
- [Hermes Agent Docker 文档](https://hermes-agent.nousresearch.com/docs/user-guide/docker)
- [Hermes Agent Providers 文档](https://hermes-agent.nousresearch.com/docs/integrations/providers)
