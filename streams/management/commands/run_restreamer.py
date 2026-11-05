"""Run the FFmpeg supervision loop as a dedicated process."""

import sys

from django.core.management.base import BaseCommand

from streams.worker import Worker


class Command(BaseCommand):
    help = "启动抖音 → YouTube 推流守护进程（请作为独立进程运行，不要放在 Web 进程里）"

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="只检查 ffmpeg、设置与直播间列表后退出")

    def handle(self, *args, **options):
        worker = Worker()
        exit_code = worker.check() if options["check"] else worker.run()
        if exit_code:
            sys.exit(exit_code)
