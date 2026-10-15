# 部署说明（入口页）

本文件旧版内容包含已不再适用的 `git pull`、默认管理员密码、健康检查版本号及镜像回滚指令。为避免照过期步骤操作，完整且当前有效的部署流程以 **[docs/DEPLOY.md](docs/DEPLOY.md)** 为唯一来源。

## 当前部署事实

- 生产服务器：腾讯云 Lighthouse，`193.112.29.164`；项目目录 `/opt/personal-agent`。
- 线上更新使用本地构建前端后 tar+SSH 同步，再在服务器执行 Docker Compose 构建；国内服务器不要依赖 GitHub `git pull`。
- `config/.env` 仅保存在服务器，不随源码包同步。
- 公网现状为 HTTP；`nginx/nginx-ssl.conf` 是启用 HTTPS 时替换使用的模板，TLS 尚未配置。
- 当前 Compose 使用源码构建，未配置镜像仓库标签；旧 `scripts/rollback.sh` 不适用于此部署方式，不要使用。

健康检查地址：`http://193.112.29.164:8080/api/health`。完整同步步骤、验证方式与部署注意事项见 [docs/DEPLOY.md](docs/DEPLOY.md)。
