# Agent Workbench

一个自托管 AI Agent 的部署与集成实验仓库，使用 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 作为运行时。

> 当前定位：可复用的 Docker Compose 部署原型。网页对话界面使用 Hermes 自带 Webapp；本仓库尚未实现自定义前端或独立任务调度服务，也不包含 Hermes 上游源码。

## 内容

- `compose.yaml`：运行 Hermes Agent、Dashboard 和 Webapp；Webapp 和管理界面端口仅绑定到服务器本机。
- `config.yaml.example`：OpenAI-compatible API 中转站的配置模板。
- `.env.example`：密钥和 Dashboard 登录凭据的变量模板，不含真实凭据。

## 启动

要求已安装 Docker Engine 和 Docker Compose plugin。

```bash
mkdir -p data
cp .env.example .env
cp config.yaml.example data/config.yaml
```

编辑 `.env`，填入 API Key、Dashboard 登录密码和独立的会话签名密钥；编辑 `data/config.yaml`，将 `api` 改为你的中转站 `/v1` 地址，并把 `model.default` 改为服务商提供的准确模型 ID。然后启动：

```bash
docker compose up -d
docker compose logs -f agent
```

- Webapp：`http://127.0.0.1:9120`
- Dashboard：`http://127.0.0.1:9119`

远程使用时可通过 SSH 本地端口转发访问，例如：

```bash
ssh -N -L 9120:127.0.0.1:9120 -L 9119:127.0.0.1:9119 user@your-server
```

不要把端口直接绑定到公网。Dashboard 的用户名密码方式仅适合可信网络或 VPN，不适合直接暴露公网。若要经域名公开访问，需另行配置 TLS、反向代理和更完整的访问控制；本仓库不包含该部分。

## 数据与密钥

`data/` 是持久化目录，可能包含会话、配置、认证信息和运行数据；`.env` 包含 API Key 与 Dashboard 凭据。两者都由 `.gitignore` 排除，**不要提交到 GitHub**。备份时也要按敏感数据保管。

## 项目边界

此仓库维护的是部署模板和集成说明，不是 Hermes Agent 上游项目的分支。上游镜像使用 `latest`；正式长期运行前，建议选择并固定经过验证的镜像版本，并备份 `data/`。

## 参考

- [Hermes Agent Docker Setup](https://hermes-agent.nousresearch.com/docs/user-guide/docker)
- [Hermes Agent Providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
