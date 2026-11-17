# Agent Workbench

一个面向手机浏览器的中文自托管 AI 编程工作台。提交任务后，由服务器后台 Agent 调用 OpenAI-compatible 模型 API，在隔离项目工作区中查看和修改文件、运行命令并保存执行记录；关闭浏览器不会中断已提交任务。

另保留原有 Hermes Agent 部署模板，位于仓库根目录的 Compose 配置中。自研中文任务工作台位于 `agent-ui/`，两者可以独立部署。

## 中文任务工作台

### 首次配置

```bash
cd agent-ui
cp .env.example .env
```

编辑 `.env`，设置两个不同的随机值及 Cicadas API Key：

```bash
sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=$(openssl rand -hex 24)|; s|^APP_SECRET=.*|APP_SECRET=$(openssl rand -hex 32)|" .env
nano .env
```

保存生成的 `APP_PASSWORD` 作为登录密码，然后在 `.env` 中设置 `CICADAS_API_KEY`，并确认 `API_BASE_URL`、`MODEL` 是服务商支持的地址和模型 ID。密钥只放在服务器 `.env`，不会发送到浏览器或任务执行容器。

启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app runner
```

工作台仅监听服务器本机 `127.0.0.1:9121`。从电脑访问时，在本机建立 SSH 隧道：

```bash
ssh -N -L 9121:127.0.0.1:9121 root@YOUR_SERVER_IP
```

然后打开 `http://127.0.0.1:9121`。手机可通过支持 SSH 本地端口转发的客户端建立同样的隧道，再在手机浏览器打开本地地址。若要不经隧道直接公网访问，需先配置有效域名、HTTPS 和反向代理；不要将应用端口直接绑定到公网。

修改 `.env` 中的模型 Key 或模型 ID 后，在 `agent-ui/` 目录执行 `docker compose up -d --force-recreate app` 使配置生效。

### 功能与边界

- 中文登录页、任务提交、任务队列、状态刷新、停止任务、事件日志及结果展示。
- SQLite 和工作区使用 Docker named volume 持久化；服务重启后排队任务会继续处理，正在运行的任务会标记为中断，文件改动保留。
- 单 Worker 串行执行；每个任务最多 18 轮模型调用，最长 30 分钟。
- 排队最多 20 个任务；保留最近 200 个已结束任务及每个任务最近 100 条事件，控制长期运行时的磁盘占用。
- 工作区最大 128 MB，单文件写入最大 40 KB。项目命令在单独的无网络容器内、256 MB 临时副本中以非 root UID 执行；每次命令使用独立的临时 `HOME` / `TMPDIR`，容器禁止子进程切换进程组，命令结束后清理整个进程组，命令生成的文件不保留。Runner 只保留启动降权进程、清理临时目录和终止子进程所需的能力，容器使用只读根文件系统和资源上限，不挂载 Docker socket，也不包含模型 API Key。
- 模型调用使用 Cicadas OpenAI-compatible `/chat/completions` API。需确认账户实际提供所填的模型 ID；仓库默认值仅作示例。

### 更新与备份

```bash
docker compose down
docker compose up -d --build
```

`docker compose down` 不会删除 named volumes。保留好 `agent_data`、`agent_workspace` 两个 volume 的备份；不要运行 `docker compose down -v`，否则会删除任务记录和工作区。

## Hermes Agent 部署模板

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
