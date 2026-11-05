"""FFmpeg supervision loop.

Run with ``python manage.py run_restreamer`` as its own process (systemd unit).
The web process never spawns FFmpeg, so it is safe to run multiple WSGI workers
behind CloudFront.
"""

import logging
import os
import signal
import threading
import time
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import services
from .models import GlobalSettings, LiveSession, Room, WorkerState

logger = logging.getLogger(__name__)

VERSION = "2.0.0"
LOCK_FILE = "restreamer.lock"
LOG_RETENTION_DAYS = 7


def _acquire_lock(path: Path):
    """Cross-platform advisory lock; the OS releases it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _update_room(room_id: int, **fields) -> None:
    Room.objects.filter(pk=room_id).update(**fields)


class Worker:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._lock_handle = None
        self._threads: dict[int, threading.Thread] = {}
        self._threads_lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def run(self) -> int:
        lock_handle = _acquire_lock(Path(settings.LOG_DIR) / LOCK_FILE)
        if lock_handle is None:
            logger.error("已有推流进程在运行（%s 被占用），本次启动退出", LOCK_FILE)
            return 1
        self._lock_handle = lock_handle
        self._install_signal_handlers()
        self._cleanup_old_logs()

        state = WorkerState.load()
        state.pid = os.getpid()
        state.started_at = timezone.now()
        state.last_seen = timezone.now()
        state.note = f"v{VERSION}"
        state.save()

        logger.info("推流进程已启动 (pid=%s, version=%s)", os.getpid(), VERSION)
        try:
            while not self.stop_event.is_set():
                self._sync_rooms()
                WorkerState.objects.filter(pk=1).update(last_seen=timezone.now(), pid=os.getpid())
                close_old_connections()
                self.stop_event.wait(5)
        finally:
            self._shutdown()
        return 0

    def check(self) -> int:
        """Validate settings and list what the worker would monitor."""
        settings_obj = GlobalSettings.load()
        try:
            cfg = services.runtime_config(settings_obj)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1
        rooms = Room.objects.all()
        print(f"ffmpeg: {cfg.ffmpeg}")
        print(f"推流服务器: {settings_obj.rtmp_server}")
        print(f"默认密钥: {'已配置' if settings_obj.default_stream_key else '未配置'}")
        print(f"直播间数量: {rooms.count()}")
        for room in rooms:
            state = "启用" if room.enabled else "停用"
            ingest = "已配置" if room.resolved_ingest_url(settings_obj) else "缺少密钥"
            print(f"  [{room.pk}] {room.display_name} | {state} | {room.effective_quality(settings_obj)} | {ingest}")
        return 0

    def _install_signal_handlers(self) -> None:
        def _handle(_signum, _frame):
            logger.info("收到退出信号，正在停止推流...")
            self.stop_event.set()

        signal.signal(signal.SIGINT, _handle)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle)

    def _shutdown(self) -> None:
        self.stop_event.set()
        deadline = time.time() + 25
        with self._threads_lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=max(deadline - time.time(), 0))
        WorkerState.objects.filter(pk=1).update(last_seen=None, note="已停止")
        if self._lock_handle is not None:
            try:
                self._lock_handle.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("推流进程已退出")

    def _cleanup_old_logs(self) -> None:
        log_dir = Path(settings.SESSION_LOG_DIR)
        if not log_dir.is_dir():
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        for path in log_dir.glob("session-*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                logger.debug("清理日志失败: %s", path, exc_info=True)

    # ------------------------------------------------------------------- rooms
    def _sync_rooms(self) -> None:
        room_ids = set(Room.objects.values_list("id", flat=True))
        with self._threads_lock:
            for room_id in room_ids:
                thread = self._threads.get(room_id)
                if thread and thread.is_alive():
                    continue
                thread = threading.Thread(
                    target=self._room_loop, args=(room_id,), name=f"room-{room_id}", daemon=True
                )
                self._threads[room_id] = thread
                thread.start()
                logger.info("已开始监测直播间 id=%s", room_id)
            for room_id in list(self._threads):
                if room_id not in room_ids:
                    self._threads.pop(room_id, None)

    def _room_loop(self, room_id: int) -> None:
        try:
            while not self.stop_event.is_set():
                close_old_connections()
                room = Room.objects.filter(pk=room_id).first()
                if room is None:
                    logger.info("直播间已删除，停止监测 id=%s", room_id)
                    return

                settings_obj = GlobalSettings.load()
                try:
                    cfg = services.runtime_config(settings_obj)
                except RuntimeError as exc:
                    _update_room(room_id, status=Room.STATUS_ERROR, status_message=str(exc))
                    self._wait(room_id, settings_obj.poll_interval)
                    continue

                if not room.enabled:
                    _update_room(room_id, status=Room.STATUS_DISABLED, status_message="已停用")
                    self._take_command(room_id)
                    self._wait(room_id, 10)
                    continue
                if room.paused:
                    _update_room(room_id, status=Room.STATUS_IDLE, status_message="已暂停")
                    self._take_command(room_id)
                    self._wait(room_id, 10)
                    continue

                self._check_room(room, cfg, settings_obj)
        except Exception:  # noqa: BLE001 - keep the supervisor alive
            logger.exception("直播间线程异常退出 id=%s", room_id)
        finally:
            close_old_connections()

    def _check_room(self, room: Room, cfg: services.RuntimeConfig, settings_obj: GlobalSettings) -> None:
        quality = services.QUALITY_CODE.get(room.effective_quality(settings_obj), "OD")
        stream_info = services.resolve_stream(room.douyin_url, quality, cfg.cookie)
        _update_room(room.pk, last_checked_at=timezone.now())

        if not stream_info:
            _update_room(room.pk, status=Room.STATUS_ERROR, status_message="获取直播状态失败")
            self._wait(room.pk, cfg.poll_interval)
            return

        if not stream_info.get("is_live"):
            _update_room(room.pk, status=Room.STATUS_OFFLINE, status_message="")
            self._wait(room.pk, cfg.poll_interval)
            return

        ingest_url = room.resolved_ingest_url(settings_obj)
        if not ingest_url:
            _update_room(room.pk, status=Room.STATUS_LIVE, status_message="未配置 YouTube 串流密钥")
            self._wait(room.pk, cfg.poll_interval)
            return

        source_url, transcode = services.pick_source(stream_info, cfg.video_mode)
        if not source_url:
            _update_room(room.pk, status=Room.STATUS_LIVE, status_message="直播源不可用（可能全部为 H.265）")
            self._wait(room.pk, cfg.poll_interval)
            return

        if cfg.show_source_url:
            logger.info("%s 直播源地址: %s", room.display_name, source_url)

        self._push(room, cfg, stream_info, source_url, transcode, ingest_url)

    def _push(
        self,
        room: Room,
        cfg: services.RuntimeConfig,
        stream_info: dict,
        source_url: str,
        transcode: bool,
        ingest_url: str,
    ) -> None:
        session = LiveSession.objects.create(
            room=room,
            source_url=source_url,
            transcoded=transcode,
            status=LiveSession.STATUS_RUNNING,
        )
        settings.SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = Path(settings.SESSION_LOG_DIR) / f"session-{session.pk}.log"
        session.log_path = str(log_path)
        session.save(update_fields=["log_path"])

        quality = stream_info.get("quality") or ""
        _update_room(room.pk, status=Room.STATUS_PUSHING, status_message=f"推流中 {quality}".strip())
        logger.info("%s 开始推流到 YouTube (%s)", room.display_name, "转码" if transcode else "直通")

        stopped = False
        return_code = None
        try:
            process, log_file = services.start_ffmpeg(cfg, source_url, ingest_url, transcode, str(log_path))
        except Exception as exc:  # noqa: BLE001 - surface the reason in the UI
            logger.exception("启动 ffmpeg 失败")
            session.status = LiveSession.STATUS_FAILED
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_at"])
            _update_room(room.pk, status=Room.STATUS_ERROR, status_message=f"启动 ffmpeg 失败: {exc}"[:255])
            self._wait(room.pk, cfg.retry_interval)
            return

        try:
            while process.poll() is None:
                if self.stop_event.is_set():
                    services.terminate_process(process)
                    stopped = True
                    break
                state = Room.objects.filter(pk=room.pk).values("enabled", "paused", "command").first()
                if (
                    state is None
                    or not state["enabled"]
                    or state["paused"]
                    or state["command"] == Room.COMMAND_STOP
                ):
                    services.terminate_process(process)
                    stopped = True
                    if state is not None:
                        self._take_command(room.pk)
                    break
                time.sleep(1)
            return_code = process.wait()
        finally:
            log_file.close()

        session.ended_at = timezone.now()
        session.return_code = return_code
        session.status = LiveSession.STATUS_STOPPED if stopped else LiveSession.STATUS_FAILED
        session.save(update_fields=["ended_at", "return_code", "status"])

        if stopped:
            _update_room(room.pk, status=Room.STATUS_IDLE, status_message="推流已停止")
        else:
            _update_room(
                room.pk,
                status=Room.STATUS_LIVE,
                status_message=f"推流中断（返回码 {return_code}）",
            )
        logger.info(
            "%s 推流结束 (rc=%s, 时长 %s)", room.display_name, return_code, session.duration_text()
        )
        self._wait(room.pk, cfg.retry_interval)

    # ------------------------------------------------------------------ helpers
    def _wait(self, room_id: int, seconds: int) -> None:
        """Sleep, but wake up early when the dashboard asks for start/stop."""
        deadline = time.monotonic() + max(int(seconds), 1)
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if self._take_command(room_id):
                return
            self.stop_event.wait(2)

    def _take_command(self, room_id: int) -> str:
        row = Room.objects.filter(pk=room_id).values("command").first()
        if not row or not row["command"]:
            return ""
        command = row["command"]
        Room.objects.filter(pk=room_id).update(command="")
        return command
