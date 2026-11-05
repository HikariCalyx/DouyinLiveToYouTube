"""Import the legacy file-based configuration into the database."""

import configparser
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from streams.models import GlobalSettings, Room

QUALITY_NAMES = ("原画", "蓝光", "超清", "高清", "标清", "流畅")
TRUE_VALUES = ("是", "true", "1", "yes", "on")


def _option(parser: configparser.RawConfigParser, sections: tuple[str, ...], option: str, default: str = "") -> str:
    for section in sections:
        if parser.has_section(section) and parser.has_option(section, option):
            return parser.get(section, option).strip()
    return default


def _int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUE_VALUES


def parse_room_lines(text: str, default_quality: str) -> list[dict]:
    """Parse ``[quality,]url[,youtube target]`` lines (mirrors the old main.py)."""
    rooms = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in re.split(r"[,，]", line) if part.strip()]
        quality = default_quality
        if parts and parts[0] in QUALITY_NAMES:
            quality = parts.pop(0)
        if not parts:
            continue

        url = parts.pop(0)
        if "://" not in url:
            url = "https://" + url
        if "douyin.com" not in url:
            continue

        rooms.append({"url": url, "quality": quality, "target": parts[0] if parts else ""})
    return rooms


class Command(BaseCommand):
    help = "从 config/config.ini 与 config/URL_config.ini 导入初始设置与直播间"

    def add_arguments(self, parser):
        parser.add_argument("--config-dir", default=str(Path(settings.BASE_DIR) / "config"))
        parser.add_argument("--force", action="store_true", help="覆盖数据库中已有的设置与直播间")

    def handle(self, *args, **options):
        config_dir = Path(options["config_dir"])
        config_path = config_dir / "config.ini"
        url_path = config_dir / "URL_config.ini"
        force = options["force"]

        parser = configparser.RawConfigParser()
        if config_path.is_file():
            parser.read(config_path, encoding="utf-8-sig")
        else:
            self.stdout.write(self.style.WARNING(f"未找到 {config_path}，将只导入直播间列表"))

        existing_settings = GlobalSettings.objects.filter(pk=1).first()
        settings_obj = existing_settings or GlobalSettings.load()

        if force or existing_settings is None:
            quality = _option(parser, ("推流设置", "录制设置"), "直播画质", "")
            if quality not in QUALITY_NAMES:
                quality = _option(parser, ("推流设置", "录制设置"), "原画|超清|高清|标清|流畅", "原画")
            if quality not in QUALITY_NAMES:
                quality = "原画"

            video_mode = _option(parser, ("推流设置", "录制设置"), "视频编码", "copy").lower()
            if video_mode not in ("copy", "auto", "libx264"):
                video_mode = "copy"

            audio_mode = _option(parser, ("推流设置", "录制设置"), "音频编码", "copy").lower()
            if audio_mode not in ("copy", "aac"):
                audio_mode = "copy"

            settings_obj.cookie = _option(parser, ("抖音", "Cookie"), "抖音cookie", settings_obj.cookie)
            settings_obj.poll_interval = _int(_option(parser, ("推流设置", "录制设置"), "循环时间(秒)", "60"), 60)
            settings_obj.retry_interval = _int(_option(parser, ("推流设置", "录制设置"), "断流重试间隔(秒)", "10"), 10)
            settings_obj.default_quality = quality
            settings_obj.video_mode = video_mode
            settings_obj.video_bitrate_kbps = _int(
                _option(parser, ("推流设置", "录制设置"), "视频码率(kbps)", "6000"), 6000
            )
            settings_obj.audio_mode = audio_mode
            settings_obj.audio_bitrate = _option(parser, ("推流设置", "录制设置"), "音频码率", "160k") or "160k"
            settings_obj.rtmp_server = _option(
                parser, ("YouTube",), "RTMP服务器地址", settings_obj.rtmp_server
            )
            settings_obj.default_stream_key = _option(
                parser, ("YouTube",), "默认串流密钥", settings_obj.default_stream_key
            )
            settings_obj.show_source_url = _bool(
                _option(parser, ("推流设置", "录制设置"), "是否显示直播源地址(是/否)", "否")
            )
            settings_obj.save()
            self.stdout.write(self.style.SUCCESS("已导入全局设置"))

        if not url_path.is_file():
            self.stdout.write(self.style.WARNING(f"未找到 {url_path}，跳过直播间导入"))
            return

        entries = parse_room_lines(url_path.read_text(encoding="utf-8-sig", errors="ignore"), settings_obj.default_quality)
        created = updated = skipped = 0
        for entry in entries:
            target = entry["target"]
            defaults = {
                "quality": entry["quality"],
                "ingest_url": target if target.startswith(("rtmp://", "rtmps://")) else "",
                "stream_key": "" if target.startswith(("rtmp://", "rtmps://")) else target,
            }
            if force:
                _, was_created = Room.objects.update_or_create(douyin_url=entry["url"], defaults=defaults)
                if was_created:
                    created += 1
                else:
                    updated += 1
            else:
                _, was_created = Room.objects.get_or_create(douyin_url=entry["url"], defaults=defaults)
                if was_created:
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(
            self.style.SUCCESS(f"直播间导入完成：新增 {created}，更新 {updated}，已存在跳过 {skipped}")
        )
