# jshERP Docker 部署实操坑（已实测）

## 1. 项目路径
`cd jshERP-latest/deploy` 常因路径不对失败。项目常位于 Workbuddy 等目录下，先用以下命令定位：
```
find /Users -maxdepth 4 -name docker-compose.yml 2>/dev/null
```
确认 `deploy/docker-compose.yml` 存在再执行 `docker compose up -d --build`。

## 2. Docker 安装卡在 sudo TTY
`brew install --cask docker` 在后台/非交互终端会因内部 `sudo -E` 要 TTY 而回滚。
解决：手动挂载官方 dmg 并 cp 到 /Applications，再 `sudo ln -s` 二进制到 /usr/local/bin（sudo 走 GUI 授权框）。
- 残留死符号链接（旧 Docker 卸载遗留）会报 "already a Binary at docker-credential-ecr-login"。
  清掉 `/usr/local/bin/docker*` 等指向 /Applications/Docker.app 的死链接（移到废纸篓，不要 rm -f）。
- docker compose 插件：`docker compose`（v2）缺失时，装 Docker Desktop 即自带 v2。

## 3. VPN 掐断镜像拉取
Shadowrocket/QuickFox 等全局隧道会让 Docker VM 拉 docker.io 镜像在 token/CDN 阶段 EOF 失败
（`failed to fetch anonymous token: ... EOF`），但 curl 直连正常。
解决：拉镜像前断开 VPN（系统设置→VPN 断开），拉完可重连。

## 4. compose MySQL 初始化路径 bug（项目自身）
`deploy/docker-compose.yml` 里：
```
- ./jshERP-boot/docs/jsh_erp.sql:/docker-entrypoint-initdb.d/init.sql:ro
```
`./` 相对 deploy 目录，但 SQL 实际在 `../jshERP-boot/docs/`。结果 init.sql 指向空目录 →
`Can't initialize batch_readline - input source is a directory`。
修正：改为 `../jshERP-boot/docs/jsh_erp.sql`。
**改完必须删掉 mysql-data 卷重来**（`docker compose down` + `docker volume rm deploy_mysql-data`），否则旧卷不会重新执行 init.sql。

## 5. 关登录验证码（方便 Agent 自动登录）
配置表 `jsh_platform_config` 设 `platform_key='checkcode_flag'` 值为 `'0'`（改完立即生效，无需重启）。
验证：登录不再要求 code/uuid 参数。
