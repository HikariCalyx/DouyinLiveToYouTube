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
sudo chmod 600 /etc/douyin-live-to-youtube.env
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

```bash
cd /opt/douyin-live-to-youtube
set -a; . /etc/douyin-live-to-youtube.env; set +a

./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py collectstatic --noinput
./.venv/bin/python manage.py createsuperuser
./.venv/bin/python manage.py import_legacy_config     # 可选
./.venv/bin/python manage.py run_restreamer --check   # 校验 ffmpeg 与配置
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

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
    }
}
```

> `X-Forwarded-Proto` 必须透传，Django 通过 `SECURE_PROXY_SSL_HEADER` 判断请求是 HTTPS。

## 6. CloudFront 分配

1. **Origin**
   - Origin domain：ALB 的 DNS 名或 EC2/Nginx 的域名。
   - Origin protocol：`HTTP only`（CloudFront 到源站走内网/受安全组保护）或 `HTTPS only`（源站有证书时）。
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
| 仪表盘提示推流进程未运行 | worker 未启动或异常退出：`journalctl -u douyin-worker -n 100` |
| `database is locked` | 两个进程同时长事务；本项目已启用 WAL 与 busy_timeout，若仍出现请检查磁盘/权限 |
| `未找到 ffmpeg` | 安装 ffmpeg 或在「全局设置 → FFmpeg 路径」中填写绝对路径 |
| 推流几秒后中断 | 看直播间「日志」页的 FFmpeg 输出；常见原因是密钥错误、YouTube 已有一个流在使用该密钥 |
