"""Django admin registrations (power-user fallback for the dashboard)."""

from django.contrib import admin

from .models import GlobalSettings, LiveSession, Room, WorkerState


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("display_name", "douyin_url", "status", "enabled", "paused", "last_checked_at")
    list_filter = ("status", "enabled", "paused")
    search_fields = ("name", "douyin_url")
    readonly_fields = ("status", "status_message", "last_checked_at", "created_at", "updated_at", "command")


@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not GlobalSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LiveSession)
class LiveSessionAdmin(admin.ModelAdmin):
    list_display = ("room", "status", "started_at", "ended_at", "return_code", "transcoded")
    list_filter = ("status", "transcoded")
    readonly_fields = ("room", "status", "source_url", "transcoded", "log_path", "return_code", "started_at", "ended_at")


@admin.register(WorkerState)
class WorkerStateAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False
