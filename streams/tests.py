"""Tests for the dashboard, the streaming primitives and the legacy importer."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from . import services
from .forms import RoomForm
from .models import GlobalSettings, LiveSession, Room


def make_config(**overrides) -> services.RuntimeConfig:
    values = {
        "ffmpeg": "/usr/bin/ffmpeg",
        "cookie": "",
        "poll_interval": 60,
        "retry_interval": 10,
        "video_mode": "copy",
        "video_bitrate": 6000,
        "audio_mode": "copy",
        "audio_bitrate": "160k",
        "show_source_url": False,
    }
    values.update(overrides)
    return services.RuntimeConfig(**values)


class SourceSelectionTests(SimpleTestCase):
    H265 = "https://cdn.example/origin.flv?codec=h265"
    H264 = "https://cdn.example/uhd.flv"

    def test_is_h265(self):
        self.assertTrue(services.is_h265(self.H265))
        self.assertFalse(services.is_h265("https://cdn.example/x.flv?codec=h264"))
        self.assertFalse(services.is_h265("https://cdn.example/x.flv"))

    def test_copy_falls_back_to_h264_rendition(self):
        info = {
            "flv_url": self.H265,
            "m3u8_url": "https://cdn.example/origin.m3u8?codec=h265",
            "sources": [
                {"quality": "ORIGIN", "flv_url": self.H265, "m3u8_url": "https://cdn.example/o.m3u8?codec=h265"},
                {"quality": "UHD", "flv_url": self.H264, "m3u8_url": "https://cdn.example/uhd.m3u8"},
            ],
        }
        self.assertEqual(services.pick_source(info, "copy"), (self.H264, False))

    def test_copy_refuses_h265_only_stream(self):
        info = {"flv_url": self.H265, "m3u8_url": "https://cdn.example/o.m3u8?codec=h265"}
        self.assertEqual(services.pick_source(info, "copy"), (None, False))

    def test_auto_transcodes_h265_only_stream(self):
        info = {"flv_url": self.H265, "m3u8_url": ""}
        self.assertEqual(services.pick_source(info, "auto"), (self.H265, True))

    def test_libx264_always_transcodes(self):
        info = {"flv_url": self.H264, "m3u8_url": ""}
        self.assertEqual(services.pick_source(info, "libx264"), (self.H264, True))


class FfmpegCommandTests(SimpleTestCase):
    def test_passthrough_uses_stream_copy(self):
        command = services.build_ffmpeg_command(make_config(), "https://src", "rtmp://dst", False)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertNotIn("-ar", command)
        self.assertEqual(command[-2:], ["flv", "rtmp://dst"])

    def test_transcode_and_audio_reencode(self):
        cfg = make_config(video_mode="libx264", audio_mode="aac")
        command = services.build_ffmpeg_command(cfg, "https://src?codec=h265", "rtmp://dst", True)
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertIn("6000k", command)


class ModelTests(TestCase):
    def test_global_settings_is_singleton(self):
        first = GlobalSettings.load()
        first.poll_interval = 30
        first.save()
        second = GlobalSettings()
        second.poll_interval = 90
        second.save()
        self.assertEqual(GlobalSettings.objects.count(), 1)
        self.assertEqual(GlobalSettings.load().poll_interval, 90)

    def test_ingest_url_from_stream_key(self):
        cfg = GlobalSettings.load()
        cfg.rtmp_server = "rtmp://a.rtmp.youtube.com/live2/"
        cfg.default_stream_key = "default-key"
        cfg.save()

        room = Room.objects.create(douyin_url="https://live.douyin.com/1")
        self.assertEqual(room.resolved_ingest_url(cfg), "rtmp://a.rtmp.youtube.com/live2/default-key")

        room.stream_key = "own-key"
        self.assertEqual(room.resolved_ingest_url(cfg), "rtmp://a.rtmp.youtube.com/live2/own-key")

        room.ingest_url = "rtmp://other.example/live2/full"
        self.assertEqual(room.resolved_ingest_url(cfg), "rtmp://other.example/live2/full")

    def test_missing_key_returns_none(self):
        cfg = GlobalSettings.load()
        cfg.default_stream_key = ""
        cfg.save()
        room = Room.objects.create(douyin_url="https://live.douyin.com/2")
        self.assertIsNone(room.resolved_ingest_url(cfg))


class RoomFormTests(TestCase):
    def test_url_without_scheme_is_normalized(self):
        form = RoomForm(data={"douyin_url": "live.douyin.com/123456", "enabled": "on"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["douyin_url"], "https://live.douyin.com/123456")

    def test_non_douyin_url_rejected(self):
        form = RoomForm(data={"douyin_url": "https://www.huya.com/123", "enabled": "on"})
        self.assertFalse(form.is_valid())
        self.assertIn("douyin_url", form.errors)

    def test_ingest_url_must_be_rtmp(self):
        form = RoomForm(data={"douyin_url": "https://live.douyin.com/1", "ingest_url": "https://nope", "enabled": "on"})
        self.assertFalse(form.is_valid())
        self.assertIn("ingest_url", form.errors)


class ViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="secret123")
        self.room = Room.objects.create(douyin_url="https://live.douyin.com/42", name="测试")

    def test_dashboard_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_dashboard_renders(self):
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "测试")

    def test_healthz_is_public(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_start_and_stop_set_flags(self):
        self.client.force_login(self.user)

        self.client.post(f"/rooms/{self.room.pk}/stop/")
        self.room.refresh_from_db()
        self.assertTrue(self.room.paused)
        self.assertEqual(self.room.command, Room.COMMAND_STOP)

        self.client.post(f"/rooms/{self.room.pk}/start/")
        self.room.refresh_from_db()
        self.assertFalse(self.room.paused)
        self.assertEqual(self.room.command, Room.COMMAND_START)

    def test_status_api(self):
        self.client.force_login(self.user)
        LiveSession.objects.create(room=self.room)
        response = self.client.get("/api/status/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["rooms"]), 1)
        self.assertEqual(payload["rooms"][0]["id"], self.room.pk)
        self.assertFalse(payload["worker_ok"])

    def test_settings_update(self):
        self.client.force_login(self.user)
        cfg = GlobalSettings.load()
        response = self.client.post(
            "/settings/",
            {
                "cookie": "ttwid=1",
                "poll_interval": 90,
                "retry_interval": 15,
                "default_quality": "高清",
                "video_mode": "copy",
                "video_bitrate_kbps": 4500,
                "audio_mode": "copy",
                "audio_bitrate": "160k",
                "rtmp_server": "rtmp://a.rtmp.youtube.com/live2",
                "default_stream_key": "key",
                "ffmpeg_path": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        cfg.refresh_from_db()
        self.assertEqual(cfg.poll_interval, 90)
        self.assertEqual(cfg.default_quality, "高清")
        self.assertTrue(cfg.default_stream_key)

    def test_room_create(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/rooms/new/",
            {"douyin_url": "live.douyin.com/999", "quality": "", "stream_key": "", "ingest_url": "", "enabled": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Room.objects.filter(douyin_url="https://live.douyin.com/999").exists())


class LegacyImportTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config_dir = Path(self.tmp.name)
        (config_dir / "config.ini").write_text(
            """
[推流设置]
循环时间(秒) = 45
直播画质 = 高清
视频编码 = copy
视频码率(kbps) = 5000
音频编码 = copy
音频码率 = 160k
断流重试间隔(秒) = 12
是否显示直播源地址(是/否) = 是

[YouTube]
RTMP服务器地址 = rtmp://a.rtmp.youtube.com/live2
默认串流密钥 = default-key

[抖音]
抖音cookie = ttwid=abc
""",
            encoding="utf-8",
        )
        (config_dir / "URL_config.ini").write_text(
            "\n".join(
                [
                    "# comment",
                    "https://live.douyin.com/111",
                    "高清,https://live.douyin.com/222,own-key",
                    "https://live.douyin.com/333,rtmp://a.rtmp.youtube.com/live2/full-url",
                    "https://www.huya.com/444",
                ]
            ),
            encoding="utf-8",
        )
        self.config_dir = config_dir

    def test_imports_settings_and_rooms(self):
        call_command("import_legacy_config", config_dir=str(self.config_dir), verbosity=0)

        cfg = GlobalSettings.load()
        self.assertEqual(cfg.poll_interval, 45)
        self.assertEqual(cfg.retry_interval, 12)
        self.assertEqual(cfg.default_quality, "高清")
        self.assertEqual(cfg.video_bitrate_kbps, 5000)
        self.assertTrue(cfg.show_source_url)
        self.assertEqual(cfg.default_stream_key, "default-key")
        self.assertEqual(cfg.cookie, "ttwid=abc")

        rooms = {room.douyin_url: room for room in Room.objects.all()}
        self.assertEqual(len(rooms), 3)
        self.assertEqual(rooms["https://live.douyin.com/222"].stream_key, "own-key")
        self.assertEqual(
            rooms["https://live.douyin.com/333"].ingest_url, "rtmp://a.rtmp.youtube.com/live2/full-url"
        )

    def test_import_is_idempotent(self):
        call_command("import_legacy_config", config_dir=str(self.config_dir), verbosity=0)
        call_command("import_legacy_config", config_dir=str(self.config_dir), verbosity=0)
        self.assertEqual(Room.objects.count(), 3)
        self.assertEqual(GlobalSettings.objects.count(), 1)
