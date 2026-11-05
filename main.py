# -*- encoding: utf-8 -*-

"""
Douyin Live -> YouTube restreamer.

Monitors Douyin live rooms and relays every live stream to a YouTube RTMP
ingest endpoint with FFmpeg.
"""

import asyncio
import configparser
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from src import spider, stream
from src.logger import logger
from src.utils import Color, get_query_params

VERSION = "1.0.0"

script_path = os.path.split(os.path.realpath(sys.argv[0]))[0]
config_file = os.path.join(script_path, "config", "config.ini")
url_config_file = os.path.join(script_path, "config", "URL_config.ini")
text_encoding = "utf-8-sig"

QUALITY_NAMES = ("原画", "蓝光", "超清", "高清", "标清", "流畅")
QUALITY_CODE = {"原画": "OD", "蓝光": "BD", "超清": "UHD", "高清": "HD", "标清": "SD", "流畅": "LD"}

DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36"
)

DEFAULT_URL_CONFIG = """\
# 抖音直播间地址，每行一个。
# 格式: [画质,]直播间地址[,YouTube串流密钥或完整推流地址]
#
# 只填地址: 使用 config.ini 中的默认串流密钥与默认画质
# 例: https://live.douyin.com/123456789
#
# 指定画质:
# 例: 高清,https://live.douyin.com/123456789
#
# 指定独立串流密钥或完整推流地址:
# 例: 原画,https://live.douyin.com/123456789,abcd-efgh-ijkl-mnop
# 例: https://live.douyin.com/123456789,rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop
#
# 支持 v.douyin.com 分享链接与 douyin.com/user/... 主页链接。
# 行首加 # 可临时忽略该直播间。
"""

color_obj = Color()

stop_event = threading.Event()
active_streams: dict[str, str] = {}
active_lock = threading.Lock()


@dataclass(frozen=True)
class Config:
    ffmpeg: str
    cookie: str
    quality: str
    delay: int
    retry_delay: int
    video_mode: str
    video_bitrate: int
    audio_mode: str
    audio_bitrate: str
    rtmp_server: str
    default_key: str
    show_url: bool


@dataclass(frozen=True)
class Room:
    url: str
    quality: str
    ingest_url: str


def read_option(parser: configparser.RawConfigParser, section: str, option: str, default: str) -> str:
    if not parser.has_section(section):
        parser.add_section(section)
    if not parser.has_option(section, option):
        parser.set(section, option, default)
        with open(config_file, "w", encoding=text_encoding) as file:
            parser.write(file)
        return default
    return parser.get(section, option).strip()


def read_bool(parser: configparser.RawConfigParser, section: str, option: str, default: str = "否") -> bool:
    return read_option(parser, section, option, default).lower() in ("是", "true", "1", "yes", "on")


def read_int(parser: configparser.RawConfigParser, section: str, option: str, default: int, minimum: int) -> int:
    try:
        value = int(read_option(parser, section, option, str(default)))
    except ValueError:
        value = default
    return max(value, minimum)


def find_ffmpeg() -> str | None:
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local_ffmpeg = os.path.join(script_path, ffmpeg_name)
    if os.path.isfile(local_ffmpeg):
        return local_ffmpeg
    return shutil.which("ffmpeg")


def load_config() -> Config:
    parser = configparser.RawConfigParser()
    parser.read(config_file, encoding=text_encoding)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        logger.error("未找到 ffmpeg，请将 ffmpeg 放入程序目录或添加到 PATH 后重试")
        sys.exit(1)

    quality = read_option(parser, "推流设置", "直播画质", "原画")
    if quality not in QUALITY_NAMES:
        logger.warning(f"配置中的画质[{quality}]无法识别，已使用 原画")
        quality = "原画"

    video_mode = read_option(parser, "推流设置", "视频编码", "auto").lower()
    if video_mode not in ("auto", "copy", "libx264"):
        logger.warning(f"配置中的视频编码[{video_mode}]无法识别，已使用 auto")
        video_mode = "auto"

    audio_mode = read_option(parser, "推流设置", "音频编码", "copy").lower()
    if audio_mode not in ("copy", "aac"):
        logger.warning(f"配置中的音频编码[{audio_mode}]无法识别，已使用 copy")
        audio_mode = "copy"

    rtmp_server = read_option(parser, "YouTube", "RTMP服务器地址", "rtmp://a.rtmp.youtube.com/live2")
    default_key = read_option(parser, "YouTube", "默认串流密钥", "")

    return Config(
        ffmpeg=ffmpeg,
        cookie=read_option(parser, "抖音", "抖音cookie", ""),
        quality=quality,
        delay=read_int(parser, "推流设置", "循环时间(秒)", 60, 5),
        retry_delay=read_int(parser, "推流设置", "断流重试间隔(秒)", 10, 1),
        video_mode=video_mode,
        video_bitrate=read_int(parser, "推流设置", "视频码率(kbps)", 6000, 500),
        audio_mode=audio_mode,
        audio_bitrate=read_option(parser, "推流设置", "音频码率", "160k"),
        rtmp_server=rtmp_server,
        default_key=default_key,
        show_url=read_bool(parser, "推流设置", "是否显示直播源地址(是/否)"),
    )


def build_ingest_url(target: str, cfg: Config) -> str | None:
    target = (target or cfg.default_key).strip()
    if not target:
        return None
    if target.startswith(("rtmp://", "rtmps://")):
        return target
    return f"{cfg.rtmp_server.rstrip('/')}/{target.lstrip('/')}"


def parse_rooms(cfg: Config) -> list[Room]:
    if not os.path.isfile(url_config_file):
        with open(url_config_file, "w", encoding=text_encoding) as file:
            file.write(DEFAULT_URL_CONFIG)
        logger.warning(f"未找到 {url_config_file}，已生成示例文件，请填写抖音直播间地址后重新运行")
        return []

    rooms: list[Room] = []
    seen_urls: set[str] = set()

    with open(url_config_file, "r", encoding=text_encoding, errors="ignore") as file:
        lines = file.readlines()

    for origin_line in lines:
        line = origin_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in re.split(r"[,，]", line) if part.strip()]
        quality = cfg.quality
        if parts and parts[0] in QUALITY_NAMES:
            quality = parts.pop(0)
        if not parts:
            continue

        room_url = parts.pop(0)
        if "://" not in room_url:
            room_url = "https://" + room_url
        if "douyin.com" not in room_url:
            color_obj.print_colored(f"跳过非抖音直播间地址: {room_url}", color_obj.YELLOW)
            continue
        if room_url in seen_urls:
            continue
        seen_urls.add(room_url)

        ingest_url = build_ingest_url(parts[0] if parts else "", cfg)
        if not ingest_url:
            logger.error(f"{room_url} 未配置 YouTube 串流密钥，已跳过")
            continue

        rooms.append(Room(url=room_url, quality=quality, ingest_url=ingest_url))

    return rooms


async def _resolve(room: Room, cfg: Config) -> dict | None:
    if "v.douyin.com" in room.url or "/user/" in room.url:
        room_data = await spider.get_douyin_app_stream_data(room.url, cookies=cfg.cookie)
    else:
        room_data = await spider.get_douyin_web_stream_data(room.url, cookies=cfg.cookie)

    if not isinstance(room_data, dict):
        return None

    stream_info = await stream.get_douyin_stream_url(room_data, QUALITY_CODE[room.quality], None)
    return stream_info if isinstance(stream_info, dict) else None


def resolve_stream(room: Room, cfg: Config) -> dict | None:
    try:
        return asyncio.run(_resolve(room, cfg))
    except Exception as e:
        logger.error(f"获取直播状态失败: {room.url} - {e}")
        return None


def is_h265(url: str) -> bool:
    codec_param = get_query_params(url, "codec")
    return bool(codec_param) and str(codec_param[0]).lower() == "h265"


def candidate_sources(stream_info: dict) -> list[dict]:
    candidates = [{
        "flv_url": stream_info.get("flv_url") or "",
        "m3u8_url": stream_info.get("m3u8_url") or "",
    }]
    candidates += [
        {"flv_url": item.get("flv_url") or "", "m3u8_url": item.get("m3u8_url") or ""}
        for item in stream_info.get("sources") or []
    ]

    unique: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        source_url = candidate["flv_url"] or candidate["m3u8_url"]
        if source_url and source_url not in seen:
            seen.add(source_url)
            unique.append(candidate)
    return unique


def pick_source(stream_info: dict, cfg: Config) -> tuple[str | None, bool]:
    candidates = candidate_sources(stream_info)
    if not candidates:
        return None, False

    if cfg.video_mode == "libx264":
        candidate = candidates[0]
        return candidate["flv_url"] or candidate["m3u8_url"], True

    # YouTube only accepts H.264, so with passthrough we fall back to another
    # rendition of the same room before giving up.
    for candidate in candidates:
        if candidate["flv_url"] and not is_h265(candidate["flv_url"]):
            return candidate["flv_url"], False
        if candidate["m3u8_url"] and not is_h265(candidate["m3u8_url"]):
            return candidate["m3u8_url"], False

    if cfg.video_mode == "copy":
        logger.warning("该直播间的所有清晰度均为 H.265，YouTube 不支持，已跳过；"
                       "如需自动转码请将 视频编码 改为 auto")
        return None, False

    logger.warning("直播源均为 H.265，已按 auto 配置转为 H.264 推流")
    candidate = candidates[0]
    return candidate["flv_url"] or candidate["m3u8_url"], True


def build_ffmpeg_command(cfg: Config, source_url: str, ingest_url: str, transcode: bool) -> list[str]:
    command = [
        cfg.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-rw_timeout", "15000000",
        "-user_agent", DOUYIN_USER_AGENT,
        "-headers", "Referer: https://live.douyin.com/\r\n",
        "-protocol_whitelist", "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy",
        "-thread_queue_size", "1024",
        "-analyzeduration", "20000000",
        "-probesize", "10000000",
        "-fflags", "+discardcorrupt",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "30",
        "-re", "-i", source_url,
        "-map", "0:v:0", "-map", "0:a:0?",
    ]

    if transcode:
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-g", "50",
            "-maxrate", f"{cfg.video_bitrate}k", "-bufsize", f"{cfg.video_bitrate * 2}k",
        ]
    else:
        command += ["-c:v", "copy"]

    if cfg.audio_mode == "aac":
        command += ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "44100", "-ac", "2"]
    else:
        command += ["-c:a", "copy"]

    command += [
        "-max_muxing_queue_size", "1024",
        "-f", "flv", ingest_url,
    ]
    return command


def _pump_ffmpeg_output(process: subprocess.Popen, anchor_name: str) -> None:
    if not process.stderr:
        return
    try:
        for output_line in process.stderr:
            output_line = output_line.strip()
            if output_line:
                logger.debug(f"[{anchor_name}] {output_line}")
    except Exception:
        pass


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    except Exception:
        pass


def restream(room: Room, cfg: Config, anchor_name: str, title: str, source_url: str, transcode: bool) -> bool:
    ingest_url = room.ingest_url
    with active_lock:
        owner = active_streams.get(ingest_url)
        if owner:
            logger.error(f"{anchor_name} 无法推流: 该 YouTube 推流地址正被 {owner} 使用")
            return False
        active_streams[ingest_url] = anchor_name

    command = build_ffmpeg_command(cfg, source_url, ingest_url, transcode)
    title_suffix = f"《{title}》" if title else ""
    logger.info(f"{anchor_name}{title_suffix} 开播，开始推流到 YouTube")

    start_time = time.time()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        logger.error(f"{anchor_name} 启动 ffmpeg 失败: {e}")
        with active_lock:
            active_streams.pop(ingest_url, None)
        return False

    pump_thread = threading.Thread(target=_pump_ffmpeg_output, args=(process, anchor_name), daemon=True)
    pump_thread.start()

    try:
        while process.poll() is None:
            if stop_event.wait(0.5):
                _terminate_process(process)
                break

        return_code = process.wait()
        pump_thread.join(timeout=5)
        elapsed = int(time.time() - start_time)

        if stop_event.is_set():
            logger.info(f"{anchor_name} 推流已停止，本次时长 {elapsed} 秒")
        else:
            logger.warning(f"{anchor_name} 推流中断 (ffmpeg 退出码 {return_code}，时长 {elapsed} 秒)，将重新检测直播状态")
    finally:
        with active_lock:
            if active_streams.get(ingest_url) == anchor_name:
                active_streams.pop(ingest_url, None)

    return True


def monitor_room(room: Room, cfg: Config) -> None:
    while not stop_event.is_set():
        stream_info = resolve_stream(room, cfg)
        if not stream_info:
            stop_event.wait(cfg.delay)
            continue

        anchor_name = stream_info.get("anchor_name") or room.url
        if not stream_info.get("is_live"):
            logger.info(f"{anchor_name} 未开播，{cfg.delay} 秒后重新检测")
            stop_event.wait(cfg.delay)
            continue

        source_url, transcode = pick_source(stream_info, cfg)
        if not source_url:
            logger.warning(f"{anchor_name} 已开播，但未获取到直播源地址，{cfg.delay} 秒后重试")
            stop_event.wait(cfg.delay)
            continue

        if cfg.show_url:
            logger.info(f"{anchor_name} 直播源地址: {source_url}")

        started = restream(room, cfg, anchor_name, stream_info.get("title") or "", source_url, transcode)

        if not stop_event.is_set():
            stop_event.wait(cfg.retry_delay if started else cfg.delay)


def display_status() -> None:
    while not stop_event.is_set():
        with active_lock:
            count = len(active_streams)
            names = "、".join(active_streams.values())
        if count:
            print(f"正在推流 {count} 路: {names}")
        else:
            print("暂无直播推流，等待开播中...")
        stop_event.wait(30)


def handle_signal(_signal, _frame) -> None:
    stop_event.set()


def main() -> None:
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    os.makedirs(os.path.join(script_path, "logs"), exist_ok=True)

    print("-----------------------------------------------------")
    print("|              DouyinLiveToYouTube                 |")
    print("-----------------------------------------------------")
    print(f"版本号: {VERSION}")
    print("功能: 将抖音直播实时转发至 YouTube（FFmpeg RTMP 推流）")
    print(".....................................................")

    cfg = load_config()
    rooms = parse_rooms(cfg)
    if not rooms:
        logger.warning("没有可推流的直播间，请编辑 config/URL_config.ini 后重新运行")
        return

    logger.info(f"已加载 {len(rooms)} 个抖音直播间，循环检测间隔 {cfg.delay} 秒")

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    workers = []
    for room in rooms:
        worker = threading.Thread(target=monitor_room, args=(room, cfg), name=room.url, daemon=True)
        worker.start()
        workers.append(worker)

    display_status()

    stop_event.set()
    deadline = time.time() + 20
    for worker in workers:
        worker.join(timeout=max(deadline - time.time(), 0))

    logger.info("程序已退出")


if __name__ == "__main__":
    main()
