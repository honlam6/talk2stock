# jshERP 本地部署踩坑记录（macOS）

## 1. Docker Desktop 安装（brew 会失败，走 dmg 手动装）
- 症状：`brew install --cask docker` 在 `Linking Binary 'docker-credential-ecr-login'` 处报
  `sudo: a terminal is required to read the password` 然后整次回滚删掉 Docker.app。
  原因：brew 内部用 `sudo -E` 强制要 TTY 读密码，非交互终端给不了。
- 前置：旧 Docker 卸载残留的**死符号链接**在 `/usr/local/bin/`（docker, docker-compose,
  docker-credential-*, com.docker.cli, hub-tool, kubectl... 全部指向已删的 /Applications/Docker.app），
  brew 因 "Binary already exists" 也会冲突。需先 sudo 移到废纸篓（用户规则：禁止 rm -f，用 mv 到 ~/.Trash）。
- 解决（绕过 brew）：
  1. `curl -fL -o /tmp/Docker.dmg https://desktop.docker.com/mac/main/arm64/Docker.dmg`
  2. `hdiutil attach /tmp/Docker.dmg -nobrowse`  → 挂载在 /Volumes/Docker
  3. `cp -R /Volumes/Docker/Docker.app /Applications/`
  4. 手动建符号链接（sudo 会弹 GUI 授权框，可点）：
     `sudo ln -sfh /Applications/Docker.app/Contents/Resources/bin/docker /usr/local/bin/docker`
     同理 docker-compose 等
  5. `open /Applications/Docker.app` 等守护进程起来（`docker info` 出 Server Version 即就绪）

## 2. Shadowrocket / VPN 挡 Docker 网络
- 症状：`docker pull` 在 `auth.docker.io/token` 或 `production.cloudfront.docker.com` 处 EOF 失败；
  `curl` 直连 docker.io 却正常（200/401）。
- 原因：Docker Desktop 的 linuxkit VM 走 gvisor+vpnkit，出网被 Shadowrocket(PacketTunnel) 全局隧道接管，
  对 docker.io 的 HTTPS 握手被掐。
- 解决：断开 Shadowrocket（系统设置→VPN，或关 app）后重拉即通。拉完镜像可再连回。
- 注：scutil --nc list 看 VPN 状态；Shadowrocket 的 AppleScript disconnect 不被识别，只能 GUI/系统设置断。

## 3. docker-compose.yml 的 SQL 挂载路径 bug（项目自带）
- 原文：`./jshERP-boot/docs/jsh_erp.sql:/docker-entrypoint-initdb.d/init.sql:ro`
  `./` 相对 compose 文件目录(deploy/)，但 SQL 在 `../jshERP-boot/docs/`（上一级）。
- 不改 → mysql 初始化指向空目录 → `ERROR: Can't initialize batch_readline - may be the input source is a directory`
- 改：`../jshERP-boot/docs/jsh_erp.sql`
- 改后必须 `docker compose down` + `docker volume rm deploy_mysql-data`（VOLUME 名随目录变，用 `docker volume ls` 确认），
  否则旧空卷不会重新导入 SQL。

## 4. 镜像源（国内拉取快）
`~/.docker/daemon.json` 加 registry-mirrors（daocloud / 163 / 百度云等）。注意写完后 `open /Applications/Docker.app` 重启 daemon 才生效。

## 验证部署成功的标准
- `docker ps` 四个容器全 Up（erp-mysql/erp-redis healthy, erp-backend/erp-web 起）
- `docker exec erp-mysql mysql ... -e "SHOW TABLES; USE jsh_erp; SHOW TABLES;"` 看到 jsh_* 表且 jsh_erp 库有表
- `curl localhost:8080/` 返回 200（前端），`curl localhost:9999/jshERP-boot/doc.html` 返回 200（后端 API 文档）
