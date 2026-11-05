"""Douyin -> YouTube primitives shared by the web process and the worker.

Nothing in here touches the database; it is pure parsing/command building so it
stays easy to unit test.
"""

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings as django_settings

from src import spider, stream
from src.utils import get_query_params

logger = logging.getLogger(__name__)

QUALITY_CODE = {"原画": "OD", "蓝光": "BD", "超清": "UHD", "高清": "HD", "标清": "SD", "流畅": "LD"}

DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36"
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Snapshot of GlobalSettings needed to run one push."""

    ffmpeg: str
    cookie: str
    poll_interval: int
    retry_interval: int
    video_mode: str
    video_bitrate: int
    audio_mode: str
    audio_bitrate: str
    show_source_url: bool


def resolve_ffmpeg(configured: str = "") -> str | None:
    if configured:
        return shutil.which(configured) or (configured if os.path.isfile(configured) else None)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local_binary = Path(django_settings.BASE_DIR) / name
    if local_binary.is_file():
        return str(local_binary)
    return shutil.which("ffmpeg")


def runtime_config(settings_obj) -> RuntimeConfig:
    ffmpeg = resolve_ffmpeg(settings_obj.ffmpeg_path)
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请在全局设置中填写路径，或将其安装到 PATH")
    return RuntimeConfig(
        ffmpeg=ffmpeg,
        cookie=settings_obj.cookie,
        poll_interval=max(int(settings_obj.poll_interval), 5),
        retry_interval=max(int(settings_obj.retry_interval), 1),
        video_mode=settings_obj.video_mode,
        video_bitrate=max(int(settings_obj.video_bitrate_kbps), 500),
        audio_mode=settings_obj.audio_mode,
        audio_bitrate=settings_obj.audio_bitrate or "160k",
        show_source_url=settings_obj.show_source_url,
    )


async def _resolve(url: str, quality: str, cookie: str) -> dict | None:
    if "v.douyin.com" in url or "/user/" in url:
        room_data = await spider.get_douyin_app_stream_data(url, cookies=cookie)
    else:
        room_data = await spider.get_douyin_web_stream_data(url, cookies=cookie)

    if not isinstance(room_data, dict):
        return None

    stream_info = await stream.get_douyin_stream_url(room_data, quality, None)
    return stream_info if isinstance(stream_info, dict) else None


def resolve_stream(url: str, quality: str, cookie: str = "") -> dict | None:
    """Return the Douyin room payload (``is_live``/``flv_url``/...), or None."""
    try:
        return asyncio.run(_resolve(url, quality, cookie))
    except Exception as exc:  # noqa: BLE001 - network/parse failures are expected
        logger.warning("获取直播状态失败 %s: %s", url, exc)
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


def pick_source(stream_info: dict, video_mode: str) -> tuple[str | None, bool]:
    """Choose a playable source URL and whether FFmpeg has to transcode it.

    YouTube only accepts H.264, so with passthrough we fall back to another
    rendition of the same room before giving up.
    """
    candidates = candidate_sources(stream_info)
    if not candidates:
        return None, False

    if video_mode == "libx264":
        candidate = candidates[0]
        return candidate["flv_url"] or candidate["m3u8_url"], True

    for candidate in candidates:
        if candidate["flv_url"] and not is_h265(candidate["flv_url"]):
            return candidate["flv_url"], False
        if candidate["m3u8_url"] and not is_h265(candidate["m3u8_url"]):
            return candidate["m3u8_url"], False

    if video_mode == "copy":
        logger.warning("该直播间的所有清晰度均为 H.265，YouTube 不支持，已跳过；如需自动转码请将视频编码改为 auto")
        return None, False

    logger.warning("直播源均为 H.265，已按 auto 配置转为 H.264 推流")
    candidate = candidates[0]
    return candidate["flv_url"] or candidate["m3u8_url"], True


def build_ffmpeg_command(cfg: RuntimeConfig, source_url: str, ingest_url: str, transcode: bool) -> list[str]:
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


def start_ffmpeg(cfg: RuntimeConfig, source_url: str, ingest_url: str, transcode: bool, log_path: str):
    """Start FFmpeg, streaming its stderr into ``log_path``."""
    command = build_ffmpeg_command(cfg, source_url, ingest_url, transcode)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
        )
    except Exception:
        log_file.close()
        raise
    return process, log_file


def terminate_process(process: subprocess.Popen, timeout: int = 10) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
    except Exception:  # noqa: BLE001 - already gone
        logger.debug("终止 ffmpeg 时出现问题", exc_info=True)


def tail_lines(log_path: str, max_lines: int = 200) -> list[str]:
    path = Path(log_path)
    if not log_path or not path.is_file():
        return []
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(max(size - 64 * 1024, 0))
        content = file.read().decode("utf-8", errors="replace")
    return content.splitlines()[-max_lines:]
