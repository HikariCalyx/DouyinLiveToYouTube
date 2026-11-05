"""Data model for the dashboard.

The database is the single source of truth: the web process edits rows and the
restreamer worker (a separate process) reads and updates them. Keeping FFmpeg
supervision out of the web requests is what makes the worker safe to run next to
several WSGI workers.
"""

from django.db import models
from django.utils import timezone

QUALITY_CHOICES = [
    ("原画", "原画"),
    ("蓝光", "蓝光"),
    ("超清", "超清"),
    ("高清", "高清"),
    ("标清", "标清"),
    ("流畅", "流畅"),
]

VIDEO_MODE_CHOICES = [
    ("copy", "直接复制（不消耗 CPU）"),
    ("auto", "H.265 时自动转码"),
    ("libx264", "始终转码"),
]

AUDIO_MODE_CHOICES = [
    ("copy", "直接复制"),
    ("aac", "重新编码为 AAC"),
]


class GlobalSettings(models.Model):
    """Singleton (pk=1) holding defaults shared by every room."""

    cookie = models.TextField("抖音 Cookie", blank=True, help_text="网页端登录后的 Cookie，可降低风控概率")
    poll_interval = models.PositiveIntegerField("检测间隔（秒）", default=60)
    retry_interval = models.PositiveIntegerField("断流重试间隔（秒）", default=10)

    default_quality = models.CharField("默认画质", max_length=8, choices=QUALITY_CHOICES, default="原画")
    video_mode = models.CharField("视频编码", max_length=16, choices=VIDEO_MODE_CHOICES, default="copy")
    video_bitrate_kbps = models.PositiveIntegerField("转码视频码率上限（kbps）", default=6000)
    audio_mode = models.CharField("音频编码", max_length=8, choices=AUDIO_MODE_CHOICES, default="copy")
    audio_bitrate = models.CharField("转码音频码率", max_length=16, default="160k")

    rtmp_server = models.CharField("YouTube RTMP 服务器", max_length=255, default="rtmp://a.rtmp.youtube.com/live2")
    default_stream_key = models.CharField("默认串流密钥", max_length=255, blank=True)

    show_source_url = models.BooleanField("在日志中显示直播源地址", default=False)
    ffmpeg_path = models.CharField("FFmpeg 路径", max_length=255, blank=True, help_text="留空则使用程序目录或 PATH 中的 ffmpeg")

    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "全局设置"
        verbose_name_plural = "全局设置"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "GlobalSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "全局设置"


class Room(models.Model):
    STATUS_IDLE = "idle"
    STATUS_OFFLINE = "offline"
    STATUS_LIVE = "live"
    STATUS_PUSHING = "pushing"
    STATUS_ERROR = "error"
    STATUS_DISABLED = "disabled"
    STATUS_CHOICES = [
        (STATUS_IDLE, "空闲"),
        (STATUS_OFFLINE, "未开播"),
        (STATUS_LIVE, "已开播"),
        (STATUS_PUSHING, "推流中"),
        (STATUS_ERROR, "异常"),
        (STATUS_DISABLED, "已停用"),
    ]

    COMMAND_START = "start"
    COMMAND_STOP = "stop"

    name = models.CharField("备注名称", max_length=100, blank=True)
    douyin_url = models.URLField("抖音直播间地址", max_length=500, unique=True)
    quality = models.CharField(
        "画质", max_length=8, choices=QUALITY_CHOICES, blank=True, help_text="留空使用全局默认画质"
    )
    stream_key = models.CharField(
        "YouTube 串流密钥", max_length=255, blank=True, help_text="留空使用全局默认密钥"
    )
    ingest_url = models.CharField(
        "完整推流地址", max_length=500, blank=True, help_text="填写后忽略串流密钥，例如 rtmp://a.rtmp.youtube.com/live2/xxx"
    )
    enabled = models.BooleanField("启用", default=True)
    paused = models.BooleanField("已暂停", default=False)
    command = models.CharField(max_length=8, blank=True, default="")

    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default=STATUS_IDLE)
    status_message = models.CharField("状态说明", max_length=255, blank=True)
    last_checked_at = models.DateTimeField("最后检测时间", null=True, blank=True)

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "直播间"
        verbose_name_plural = "直播间"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.name or self.douyin_url

    def effective_quality(self, settings_obj: GlobalSettings) -> str:
        return self.quality or settings_obj.default_quality

    def ingest_target(self, settings_obj: GlobalSettings) -> str:
        return (self.ingest_url or self.stream_key or settings_obj.default_stream_key).strip()

    def resolved_ingest_url(self, settings_obj: GlobalSettings) -> str | None:
        target = self.ingest_target(settings_obj)
        if not target:
            return None
        if target.startswith(("rtmp://", "rtmps://")):
            return target
        return f"{settings_obj.rtmp_server.rstrip('/')}/{target.lstrip('/')}"

    def active_session(self):
        return self.sessions.filter(ended_at__isnull=True).order_by("-started_at").first()


class LiveSession(models.Model):
    STATUS_RUNNING = "running"
    STATUS_STOPPED = "stopped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "推流中"),
        (STATUS_STOPPED, "已停止"),
        (STATUS_FAILED, "异常结束"),
    ]

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="sessions")
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    source_url = models.TextField("直播源地址", blank=True)
    transcoded = models.BooleanField("是否转码", default=False)
    log_path = models.CharField("日志文件", max_length=500, blank=True)
    return_code = models.IntegerField("FFmpeg 返回码", null=True, blank=True)
    started_at = models.DateTimeField("开始时间", auto_now_add=True)
    ended_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        verbose_name = "推流记录"
        verbose_name_plural = "推流记录"
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.room.display_name} @ {self.started_at:%Y-%m-%d %H:%M:%S}"

    @property
    def duration_seconds(self) -> int:
        end = self.ended_at or timezone.now()
        return max(int((end - self.started_at).total_seconds()), 0)

    def duration_text(self) -> str:
        total = self.duration_seconds
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


class WorkerState(models.Model):
    """Heartbeat of the `run_restreamer` process, used by the dashboard banner."""

    pid = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "推流进程状态"
        verbose_name_plural = "推流进程状态"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "WorkerState":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def is_alive(self, within_seconds: int = 45) -> bool:
        if not self.last_seen:
            return False
        return (timezone.now() - self.last_seen).total_seconds() <= within_seconds

    def __str__(self) -> str:
        return "推流进程状态"
