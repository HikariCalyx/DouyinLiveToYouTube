# DouyinLiveToYouTube

将**抖音直播间**的实时画面，通过 **FFmpeg** 转发（restream）到 **YouTube 直播**。

程序会持续检测 `config/URL_config.ini` 中填写的抖音直播间状态：开播后自动拉取直播源并推流到 YouTube RTMP 地址，下播后停止推流并继续等待，无需人工干预。

> 本项目由 [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 精简改造而来，仅保留抖音直播解析与 FFmpeg 推流能力。

## 功能

- 抖音直播间状态检测（网站直播间链接、`v.douyin.com` 分享链接、用户主页链接）
- 开播自动推流、下播自动停止、断流自动重连
- 支持多直播间同时转发（每个直播间对应一个 YouTube 串流密钥）
- 画质选择：原画 / 蓝光 / 超清 / 高清 / 标清 / 流畅
- 编码策略：默认 `copy` 直通（视频、音频均不重新编码，几乎不占 CPU）；抖音原画为 H.265 时自动切换同一直播间的 H.264 清晰度；`auto` 可改为转码 H.264

## 环境要求

- Python 3.10+
- FFmpeg（已放入程序目录，或已加入 `PATH`）

## 安装

```bash
pip install -r requirements.txt
```

## 配置

### 1. `config/config.ini`

| 配置项 | 说明 |
| --- | --- |
| `循环时间(秒)` | 检测直播状态的间隔，建议不低于 60 秒 |
| `直播画质` | 默认画质，`原画` / `蓝光` / `超清` / `高清` / `标清` / `流畅` |
| `视频编码` | `copy`（默认，直接复制不转码）/ `auto`（H.265 时转码）/ `libx264`（始终转码） |
| `视频码率(kbps)` | 转码时的视频码率上限，仅转码时生效 |
| `音频编码` | `copy`（默认，直接复制）/ `aac`（重新编码） |
| `音频码率` | 重新编码音频时的码率，YouTube 建议 128k 以上 |
| `断流重试间隔(秒)` | 推流中断后重新检测的等待时间 |
| `是否显示直播源地址(是/否)` | 是否在日志中打印抖音直播源地址 |
| `RTMP服务器地址` | 默认 `rtmp://a.rtmp.youtube.com/live2` |
| `默认串流密钥` | YouTube 直播的串流密钥 |
| `抖音cookie` | 网页端登录后的 Cookie，建议填写以降低风控概率 |

### 2. `config/URL_config.ini`

每行一个直播间，格式为 `[画质,]直播间地址[,YouTube串流密钥或完整推流地址]`：

```ini
# 使用默认密钥与默认画质
https://live.douyin.com/123456789

# 单独指定画质
高清,https://live.douyin.com/123456789

# 单独指定串流密钥
原画,https://live.douyin.com/123456789,abcd-efgh-ijkl-mnop

# 直接填写完整推流地址
https://live.douyin.com/123456789,rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop

# 行首加 # 可临时忽略该直播间
# https://live.douyin.com/987654321
```

## 运行

```bash
python main.py
```

修改配置或直播间列表后需要重启程序生效。按 `Ctrl+C` 可安全退出（正在推流的 FFmpeg 会被正常终止）。

## 注意事项

- 一个 YouTube 串流密钥同一时间只能承载一路直播，多个直播间请分别配置不同的密钥，否则程序会跳过冲突的推流并记录错误日志。
- YouTube 要求视频编码为 H.264、音频为 AAC。默认 `copy` 直通时，若原画为 H.265，程序会自动改用该直播间其它 H.264 清晰度；若所有清晰度都是 H.265，则跳过该直播间（可将 `视频编码` 改为 `auto` 自动转码）。
- 直通模式要求抖音音轨为 AAC（绝大多数直播间如此）。若日志出现音频相关错误，将 `音频编码` 改为 `aac`。
- 长间隔（60 秒以上）可以降低抖音接口风控概率，代价是最多延迟一个检测周期才开播。
- 日志与错误信息保存在 `logs/` 目录。

## 项目结构

```
.
├── config
│   ├── config.ini        # 推流、编码与 YouTube 配置
│   └── URL_config.ini    # 抖音直播间列表
├── logs                  # 运行日志
├── src
│   ├── ab_sign.py        # 抖音 a_bogus 签名
│   ├── http_clients      # HTTP 请求封装
│   ├── logger.py         # 日志
│   ├── room.py           # 分享链接 / 主页链接解析
│   ├── spider.py         # 抖音直播间数据解析
│   ├── stream.py         # 直播源地址选择
│   └── utils.py          # 通用工具
├── main.py               # 主程序：监测、推流调度、FFmpeg 进程管理
└── requirements.txt
```

## 许可与致谢

本项目基于 [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)（作者 Hmily）精简改造，遵循 MIT 协议，见 [LICENSE](LICENSE)。
