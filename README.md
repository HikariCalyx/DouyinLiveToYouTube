# DouyinLiveToYouTube

把**抖音直播间**的实时画面，通过 **FFmpeg** 转发（restream）到 **YouTube 直播**，并提供一个 **Django 仪表盘**来管理直播间与推流状态。生产环境通过 **CloudFront** 反向代理对外提供服务。

> 本项目由 [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)（作者 Hmily）精简改造而来，仅保留抖音直播解析与 FFmpeg 推流能力，并重构为 Web 后台。

## 架构

```
浏览器 ──HTTPS──▶ CloudFront ──▶ ALB / Nginx ──▶ gunicorn (Django)  ─┐
                                                                     │ 读写
                                                                 SQLite (WAL)
                                                                     │ 读写
                                       run_restreamer 进程 ──▶ FFmpeg ──RTMP──▶ YouTube
```

- **Web 进程只读写数据库**，不会启动 FFmpeg，因此可以安全地跑多个 gunicorn worker。
- **推流进程**（`manage.py run_restreamer`）是唯一管理 FFmpeg 子进程的地方，用文件锁保证单实例。
- 两者通过 SQLite 共享状态：网页改数据库，守护进程轮询并更新状态。

## 功能

- 登录后可见的直播间仪表盘：状态、推流时长、错误信息自动刷新
- 直播间增删改，可单独设置画质、YouTube 串流密钥或完整推流地址
- 手动「开始 / 停止」推流（停止后保持暂停，直到再次点击开始）
- 全局设置：抖音 Cookie、检测间隔、默认画质、编码策略、码率、RTMP 服务器、默认密钥、FFmpeg 路径
- 默认**零转码**直通：视频 `-c:v copy`，音频 `-c:a copy`
- 健康检查接口 `/healthz`，供 ALB / CloudFront 探测

## 环境要求

- Python 3.10 及以上（Django 5.2 LTS）
- FFmpeg（放在项目目录或加入 `PATH`）

## 本地快速开始

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser
python manage.py import_legacy_config    # 可选：导入旧的 config/*.ini
python manage.py run_restreamer          # 终端 A：推流守护进程
python manage.py runserver               # 终端 B：Web 后台
```

打开 <http://127.0.0.1:8000>，登录后即可添加抖音直播间并配置 YouTube 串流密钥。

> 本地调试请设置 `DJANGO_DEBUG=1`。生产部署见 [docs/deployment.md](docs/deployment.md)。

## 运行机制

- 推流进程每 5 秒同步一次直播间列表：新增的房间自动开始监测，删除的房间线程自动退出。
- 每个直播间一个线程：按「检测间隔」查询开播状态；开播后启动 FFmpeg 推流，下播后继续等待。
- 「停止」写入 `paused=True` 并唤醒线程，立即结束当前 FFmpeg，且不再自动重连；「开始」清除暂停并立即重新检测。
- 同一个 YouTube 串流密钥同一时间只能推一路，冲突时后启动的房间会被跳过并在界面提示。
- 编码策略：
  - `copy`（默认）：视频/音频都不重新编码，几乎不占 CPU；
  - `auto`：仅当直播源为 H.265 时转码为 H.264（YouTube 不接受 H.265）；
  - `libx264`：始终转码，可用「转码视频码率上限」限制上传带宽。
- `copy` 模式下如果原画是 H.265，会自动切换到该直播间其它 H.264 清晰度；若全部是 H.265 则跳过该直播间。

## 配置来源

运行配置全部在数据库里，通过网页维护。`config/config.ini` 与 `config/URL_config.ini` 只供 `import_legacy_config` 首次导入使用：

| 旧文件 | 导入内容 |
| --- | --- |
| `config/config.ini` | Cookie、检测间隔、画质、编码、码率、RTMP 服务器、默认密钥 |
| `config/URL_config.ini` | 每行 `[画质,]直播间地址[,串流密钥或完整推流地址]`，`#` 开头为注释 |

重复执行不会覆盖已有数据，加 `--force` 才会覆盖。

### 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django 密钥，生产必须设置 | 开发用占位值 |
| `DJANGO_DEBUG` | 调试模式 | `0` |
| `DJANGO_ALLOWED_HOSTS` | 允许的主机名，逗号分隔 | `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 允许提交表单的来源（含协议） | 空 |
| `DJANGO_HTTPS` | 安全 Cookie（HTTPS 部署设为 1） | 非 DEBUG 时为 `1` |
| `DJANGO_SECURE_SSL_REDIRECT` | 是否由 Django 做 HTTPS 跳转 | `0` |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS 秒数 | `0` |
| `DJANGO_ADMIN_PATH` | Django admin 路径 | `django-admin/` |
| `DJANGO_DB_PATH` | SQLite 文件路径 | `db.sqlite3` |
| `DJANGO_LOG_DIR` | 日志与会话日志目录 | `logs/` |
| `DJANGO_TIME_ZONE` | 时区 | `Asia/Shanghai` |
| `DJANGO_LOG_LEVEL` | 日志级别 | `INFO` |

## 常用命令

```bash
python manage.py run_restreamer --check   # 校验 ffmpeg / 设置 / 直播间后退出
python manage.py collectstatic            # 生成静态文件（WhiteNoise 使用）
python manage.py import_legacy_config --force --config-dir ./config
```

## 项目结构

```
.
├── dashboard/              # Django 项目（settings / urls / wsgi）
├── streams/
│   ├── models.py           # GlobalSettings / Room / LiveSession / WorkerState
│   ├── services.py         # 抖音解析、流选择、FFmpeg 命令（纯函数，可测试）
│   ├── worker.py           # 推流守护进程（线程 + 数据库状态机）
│   ├── views.py            # 仪表盘、房间 CRUD、启停、状态 API、健康检查
│   ├── management/commands # run_restreamer / import_legacy_config
│   ├── tests.py            # 单元测试与视图测试
│   ├── templates/          # 页面模板
│   └── static/             # 样式
├── src/                    # 抖音直播间解析（ab_sign / spider / stream / room）
├── deploy/                 # systemd 单元与环境变量示例
├── docs/                   # 部署文档
├── config/                 # 旧版 ini（仅用于导入）
├── manage.py
└── requirements.txt
```

## 部署

部署到 AWS（EC2 / ALB + CloudFront）的完整步骤见 **[docs/deployment.md](docs/deployment.md)**。

## 许可与致谢

基于 [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)（作者 Hmily）精简改造，遵循 MIT 协议，见 [LICENSE](LICENSE)。
