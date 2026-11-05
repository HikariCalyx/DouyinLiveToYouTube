# 生产部署（CloudFront 反向代理）

目标拓扑：

```
Viewer ──HTTPS──▶ CloudFront ──▶ 源站(ALB 或 Nginx/EC2) ──▶ gunicorn (Django) ──▶ SQLite
                                                                       ▲
                                          run_restreamer (systemd) ─────┘  → FFmpeg → YouTube RTMP
```

CloudFront 只做 TLS 终止、缓存与边缘接入；Django 仍然是唯一应用源站。Web 进程与推流进程跑在同一台机器上，通过 SQLite（WAL）共享状态。

---

## 1. 服务器准备

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg
sudo mkdir -p /opt/douyin-live-to-youtube /var/log/douyin-live /var/lib/douyin-live
sudo chown -R www-data:www-data /opt/douyin-live-to-youtube /var/log/douyin-live /var/lib/douyin-live

# 部署代码后
cd /opt/douyin-live-to-youtube
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## 2. 环境变量

把 `deploy/env.example` 复制到 `/etc/douyin-live-to-youtube.env` 并修改：

```bash
sudo cp deploy/env.example /etc/douyin-live-to-youtube.env
sudo chown root:www-data /etc/douyin-live-to-youtube.env
sudo chmod 640 /etc/douyin-live-to-youtube.env   # systemd 与应用用户可读，其他用户不可读
```

关键项：

| 变量 | 说明 |
| --- | --- |
| `DJANGO_SECRET_KEY` | 随机 50+ 字符，可用 `python -c "import secrets;print(secrets.token_urlsafe(50))"` |
| `DJANGO_ALLOWED_HOSTS` | 必须包含 CloudFront 域名与自定义域名 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 必须包含 `https://` 前缀的来源，否则表单提交会 403 |
| `DJANGO_HTTPS=1` | 让会话/CSRF Cookie 带 Secure 标记 |
| `DJANGO_DB_PATH` | 建议放到 `/var/lib/douyin-live/db.sqlite3` |
| `DJANGO_LOG_DIR` | 建议 `/var/log/douyin-live` |

## 3. 初始化

> **必须用与服务相同的用户（`www-data`）执行初始化。** 用 root 执行会让 `db.sqlite3`、`db.sqlite3-wal`、
> `db.sqlite3-shm` 变成 root 所有，之后服务以 `www-data` 运行，登录时写会话会 500：
> `attempt to write a readonly database`。

```bash
cd /opt/douyin-live-to-youtube
sudo -u www-data bash -c '
  set -a; . /etc/douyin-live-to-youtube.env; set +a
  ./.venv/bin/python manage.py migrate
  ./.venv/bin/python manage.py collectstatic --noinput
  ./.venv/bin/python manage.py import_legacy_config      # 可选
  ./.venv/bin/python manage.py run_restreamer --check
'
sudo -u www-data ./.venv/bin/python manage.py createsuperuser   # 需要交互输入
```

## 4. gunicorn 与 systemd

```bash
sudo cp deploy/systemd/douyin-web.service /etc/systemd/system/
sudo cp deploy/systemd/douyin-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-web douyin-worker
sudo systemctl status douyin-web douyin-worker
```

- Web：`gunicorn dashboard.wsgi:application`，监听 `127.0.0.1:8000`，不直接对外。
- Worker：`python manage.py run_restreamer`，只允许一个实例（`logs/restreamer.lock` 加锁），重复启动会立即退出。
- 两者以同一用户（如 `www-data`）运行，才能同时读写 SQLite 与日志目录。

SQLite 并发有限，建议 `--workers 2 --threads 4`；本项目的写入量很小（状态更新每秒几次），完全够用。

## 5. 源站：ALB 或 Nginx

### 方案 A：ALB（推荐）

1. 创建 Target Group，协议 HTTP、端口 8000，健康检查路径 `/healthz`，成功码 `200`。
2. 注册 EC2 实例。
3. ALB 监听 80（或 443 + ACM 证书），转发到该 Target Group。
4. 安全组只允许 CloudFront 访问 ALB：使用 AWS 托管前缀列表 `com.amazonaws.global.cloudfront.net`。

### 方案 B：Nginx 直接反代

仓库已提供可直接使用的配置：[deploy/nginx/livemgr.conf](../deploy/nginx/livemgr.conf)。

```bash
sudo cp deploy/nginx/livemgr.conf /etc/nginx/sites-available/livemgr
sudo ln -sfn /etc/nginx/sites-available/livemgr /etc/nginx/sites-enabled/livemgr
sudo rm -f /etc/nginx/sites-enabled/default      # 否则默认站点会抢走所有请求
sudo nginx -t && sudo systemctl reload nginx
```

**访问到 "Welcome to nginx!" 的三个原因**（按概率排序）：

1. `sites-available` 只是存放目录，nginx 只会加载 `sites-enabled` 中的文件/软链接；
   只放文件、不建链接，等于没有这份配置。
2. 即使建了链接，Debian/Ubuntu 自带的 `default` 站点带有 `listen 80 default_server;`，
   会优先处理 `server_name` 不匹配的请求（CloudFront 回源时 Host 是分配域名），必须删掉。
3. nginx.conf 中没有 `include /etc/nginx/sites-enabled/*;`（RHEL/CentOS 系默认没有），
   此时应把配置放到 `/etc/nginx/conf.d/livemgr.conf`。

自检：

```bash
sudo nginx -T | grep -nE 'include .*(sites-enabled|conf\.d)|listen |server_name'
sudo ss -lntp | grep ':80'
curl -sI -H 'Host: <cloudfront-domain>' http://127.0.0.1/ | head -n 1   # 应是 302 或 200，不是 Welcome 页
```

> `X-Forwarded-Proto` 必须透传，Django 通过 `SECURE_PROXY_SSL_HEADER` 判断请求是 HTTPS；
> 缺失会导致登录后反复跳转或表单 CSRF 403。若 nginx 前面还有 ALB，同样要保留该头部。

### 方案 C：不用 Nginx，CloudFront 直连 gunicorn

可行，`8000` 也是 CloudFront 允许的自定义源站端口（有效值：`80`、`443`、`1024–65535`），
但下面三点必须同时满足，否则会 502 或把源站暴露在公网。

1. **显式设置源站端口**。CloudFront 的默认 HTTP 端口是 80，不改连不上 gunicorn：
   Origin → **Protocol** = `HTTP only`，**HTTP port** = `8000`。
2. **收紧安全组**。`0.0.0.0:8000` 会把 gunicorn 直接暴露到公网，而 CloudFront 不会对源站做身份验证
   （没有签名、没有 mTLS），任何人拿到 IP 就能绕过 CloudFront 直连（此时没有 TLS、WAF、地理限制）。
   安全组只放行 AWS 托管前缀列表 `com.amazonaws.global.cloudfront.net` 的 8000 端口。
3. **`DJANGO_ALLOWED_HOSTS` 要匹配 CloudFront 发来的 Host**。默认 CloudFront 把 Host 替换成源站域名；
   使用 `AllViewer` 原点请求策略时转发的是浏览器 Host（CloudFront 域名 / 自定义域名）。
   稳妥做法是把 CloudFront 域名、自定义域名和源站域名都列进去。

切换方式（单位文件已支持 `DJANGO_BIND`）：

```bash
# /etc/douyin-live-to-youtube.env
DJANGO_BIND=0.0.0.0:8000

sudo systemctl daemon-reload && sudo systemctl restart douyin-web
sudo ss -lntp | grep 8000
curl -sI http://<cloudfront-domain>/ | head -n 1     # 期望 302/200，不要 502
```

与 Nginx 方案相比会失去：请求缓冲与慢客户端保护、限流、静态文件卸载（WhiteNoise 仍能正常提供）、
以及健康检查能力（CloudFront 本身不做源站健康检查，需外部监控；`/healthz` 仍可用于监控或 ALB）。

> 若希望进一步防止绕过 CloudFront，可在分配中加一个自定义来源请求头
> （如 `X-Origin-Verify: <随机值>`），并在 Django 中校验；本项目默认未启用。

## 6. CloudFront 分配

1. **Origin**
   - Origin domain：ALB 的 DNS 名或 EC2/Nginx 的域名。
   - Origin protocol：`HTTP only`（CloudFront 到源站走内网/受安全组保护）或 `HTTPS only`（源站有证书时）。
   - Origin HTTP port：默认 80；直连 gunicorn（`0.0.0.0:8000`）时必须填 `8000`（CloudFront 支持 80、443、1024–65535）。
2. **Viewer protocol policy**：`Redirect HTTP to HTTPS`（或 `HTTPS only`）。
3. **Allowed HTTP methods**：`GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE`。
   表单提交是 POST，必须包含。
4. **Cache policy**：`CachingDisabled`（AWS 托管）。
   页面包含 CSRF Token 与会话 Cookie，绝不能缓存。
5. **Origin request policy**：`AllViewer`（AWS 托管）。
   必须把 `Host`、`Cookie`、`Origin`、`Referer`、`X-Forwarded-Proto` 等原样转发给源站。
6. **Response headers policy**（可选）：开启 HSTS（`Strict-Transport-Security`）、`X-Content-Type-Options` 等。
7. **Custom domain**：ACM 证书必须建在 **us-east-1**，然后在 CloudFront 绑定 CNAME。
8. **压缩**：开启 Compress objects automatically。

### 静态文件

WhiteNoise 直接由 Django 提供 `/static/`（已压缩），CloudFront 只需把请求转发过去即可。
如希望边缘缓存静态资源，可加一条缓存行为：

- Path pattern：`/static/*`
- Cache policy：`CachingOptimized`
- Origin request policy：`None`

> 本项目使用 `CompressedStaticFilesStorage`（文件名不带哈希）。若开启边缘缓存，更新样式后需要创建 invalidation：`aws cloudfront create-invalidation --distribution-id <ID> --paths "/static/*"`。

### 健康检查

- ALB Target Group：`GET /healthz`，期望 `200`。
- 该路径不需要登录，返回纯文本 `ok`（数据库不可用时返回 `503`）。

## 7. 上线前检查清单

- [ ] `DJANGO_SECRET_KEY` 已改为随机值，且文件权限 `600`
- [ ] `DJANGO_DEBUG=0`
- [ ] `DJANGO_ALLOWED_HOSTS` 包含 CloudFront 域名 / 自定义域名
- [ ] `DJANGO_CSRF_TRUSTED_ORIGINS` 包含 `https://...` 来源（缺一个就会 403）
- [ ] `DJANGO_HTTPS=1`（Cookie 带 Secure）
- [ ] 源站 8000 端口只对 ALB / 本机开放，不暴露公网
- [ ] 已执行 `collectstatic`
- [ ] 管理员密码足够强，`DJANGO_ADMIN_PATH` 可改为自定义路径
- [ ] 定期备份 SQLite 文件与 `DJANGO_LOG_DIR`

## 8. 备份与升级

```bash
# 备份（WAL 模式下建议用 sqlite3 在线备份）
sqlite3 /var/lib/douyin-live/db.sqlite3 ".backup '/var/backups/douyin-$(date +%F).sqlite3'"

# 升级
cd /opt/douyin-live-to-youtube
git pull
./.venv/bin/pip install -r requirements.txt
set -a; . /etc/douyin-live-to-youtube.env; set +a
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart douyin-web douyin-worker
```

推流进程重启会结束正在进行的推流，随后按检测间隔自动重新开播（抖音直播仍在播的情况下）。

## 9. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 表单提交 403 `CSRF verification failed` | `DJANGO_CSRF_TRUSTED_ORIGINS` 缺少当前访问域名（要带 `https://`） |
| 页面反复跳登录 / 登录后仍 302 | `DJANGO_HTTPS=1` 但实际用 HTTP 访问；本地调试请设 `DJANGO_DEBUG=1` |
| `/static/...` 404 | 忘记 `collectstatic`，或 Nginx/ALB 没把 `/static/` 交给 Django |
| 502 Bad Gateway | gunicorn 未启动或端口不对，`systemctl status douyin-web` |
| 登录 500，日志 `attempt to write a readonly database` | SQLite 数据库**文件或其所在目录**对运行用户不可写（WAL 还要在同目录创建 `-wal`/`-shm`）。按下面「权限修复」处理 |
| 直接访问源站显示 `Welcome to nginx!` | 配置只在 `sites-available` 未软链到 `sites-enabled`，或 `sites-enabled/default` 仍占用 `default_server`，见「方案 B」 |
| 访问 `http://<域名>:8000` 返回 `Connection header did not include 'upgrade'` | 该端口不是 gunicorn 在应答（Django/gunicorn 不会输出这句话，它来自 Python `websockets` 库的握手校验）。用 `sudo ss -lntp \| grep :8000` 找出真正的占用者；常见情况是同一个 8000 端口上还绑了面板/WebSocket 服务（`127.0.0.1` 与公网 IP 可以并存） |
| 仪表盘提示推流进程未运行 | worker 未启动或异常退出：`journalctl -u douyin-worker -n 100` |
| `database is locked` | 两个进程同时长事务；本项目已启用 WAL 与 busy_timeout，若仍出现请检查磁盘/权限 |
| `未找到 ffmpeg` | 安装 ffmpeg 或在「全局设置 → FFmpeg 路径」中填写绝对路径 |
| 推流几秒后中断 | 看直播间「日志」页的 FFmpeg 输出；常见原因是密钥错误、YouTube 已有一个流在使用该密钥 |

### 权限修复（`attempt to write a readonly database`）

先停掉两个服务，避免边改边写：

```bash
sudo systemctl stop douyin-web douyin-worker

DB=/var/lib/douyin-live/db.sqlite3
sudo mkdir -p "$(dirname "$DB")"
sudo chown -R www-data:www-data "$(dirname "$DB")"
sudo chmod 775 "$(dirname "$DB")"          # 目录必须可写（WAL 需要创建 -wal/-shm）
sudo chmod 664 "$DB" "$DB"-wal "$DB"-shm 2>/dev/null

sudo systemctl start douyin-web douyin-worker
```

以运行用户的身份验证（比看 `ls -l` 更可靠）：

```bash
sudo -u www-data test -w "$(dirname "$DB")" && echo dir-writable
sudo -u www-data sqlite3 "$DB" "CREATE TABLE _probe(x); DROP TABLE _probe;" && echo db-writable
sudo -u www-data sqlite3 "$DB" "PRAGMA journal_mode;"        # 期望 wal
namei -l "$DB"                                               # 逐级检查父目录权限
findmnt -T "$DB"                                             # 确认不是只读挂载
```

常见根因：

- `migrate` / `createsuperuser` 用 root 跑过 → 数据库文件属主是 root；
- 只 `chown` 了数据库文件，没有 `chown` 目录 → 无法创建 `-wal`/`-shm`；
- 数据库放在 NFS/SMB/容器挂载卷上 → SQLite 无法正确加锁，换到本地磁盘；
- 启用了 SELinux：`sudo chcon -R -t httpd_var_lib_t "$(dirname "$DB")"` 或 `ausearch -m avc -ts recent` 查拒绝记录。
