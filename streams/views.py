"""HTTP views for the dashboard."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, TemplateView, UpdateView

from .forms import RoomForm, SettingsForm
from .models import GlobalSettings, LiveSession, Room, WorkerState
from .services import resolve_ffmpeg, tail_lines


def _running_sessions() -> dict[int, LiveSession]:
    return {session.room_id: session for session in LiveSession.objects.filter(ended_at__isnull=True)}


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "streams/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rooms = list(Room.objects.all())
        running = _running_sessions()
        for room in rooms:
            room.running_session = running.get(room.pk)

        worker_state = WorkerState.load()
        context.update(
            {
                "rooms": rooms,
                "global_settings": GlobalSettings.load(),
                "worker_state": worker_state,
                "worker_ok": worker_state.is_alive(),
            }
        )
        return context


class RoomCreateView(LoginRequiredMixin, CreateView):
    model = Room
    form_class = RoomForm
    template_name = "streams/room_form.html"
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        messages.success(self.request, "直播间已添加")
        return super().form_valid(form)


class RoomUpdateView(LoginRequiredMixin, UpdateView):
    model = Room
    form_class = RoomForm
    template_name = "streams/room_form.html"
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        messages.success(self.request, "直播间已更新")
        return super().form_valid(form)


class RoomDeleteView(LoginRequiredMixin, DeleteView):
    model = Room
    template_name = "streams/room_confirm_delete.html"
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        messages.success(self.request, f"已删除：{self.object.display_name}")
        return super().form_valid(form)


class RoomStartView(LoginRequiredMixin, View):
    def post(self, request, pk: int):
        room = get_object_or_404(Room, pk=pk)
        Room.objects.filter(pk=room.pk).update(paused=False, command=Room.COMMAND_START)
        messages.success(request, f"已请求立即开始：{room.display_name}")
        return redirect("dashboard")


class RoomStopView(LoginRequiredMixin, View):
    def post(self, request, pk: int):
        room = get_object_or_404(Room, pk=pk)
        Room.objects.filter(pk=room.pk).update(paused=True, command=Room.COMMAND_STOP)
        messages.success(request, f"已停止：{room.display_name}")
        return redirect("dashboard")


class SettingsView(LoginRequiredMixin, UpdateView):
    model = GlobalSettings
    form_class = SettingsForm
    template_name = "streams/settings.html"
    success_url = reverse_lazy("dashboard")

    def get_object(self, queryset=None):
        return GlobalSettings.load()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["resolved_ffmpeg"] = resolve_ffmpeg(self.object.ffmpeg_path) or ""
        return context

    def form_valid(self, form):
        messages.success(self.request, "全局设置已保存")
        return super().form_valid(form)


class SessionLogView(LoginRequiredMixin, TemplateView):
    template_name = "streams/log.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = get_object_or_404(LiveSession.objects.select_related("room"), pk=self.kwargs["pk"])
        context.update({"session": session, "lines": tail_lines(session.log_path)})
        return context


@login_required
def status_api(request):
    running = _running_sessions()
    rooms = []
    for room in Room.objects.all():
        session = running.get(room.pk)
        rooms.append(
            {
                "id": room.pk,
                "status": room.status,
                "status_label": room.get_status_display(),
                "message": room.status_message,
                "last_checked": room.last_checked_at.isoformat() if room.last_checked_at else None,
                "duration": session.duration_text() if session else "",
            }
        )
    worker_state = WorkerState.load()
    return JsonResponse({"rooms": rooms, "worker_ok": worker_state.is_alive()})


def healthz(request):
    """Origin health check (no auth, no sensitive data)."""
    try:
        Room.objects.exists()
    except Exception:  # noqa: BLE001 - report DB problems to the load balancer
        return HttpResponse("db-error", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
