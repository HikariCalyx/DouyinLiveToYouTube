"""URL routes for the dashboard."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("rooms/new/", views.RoomCreateView.as_view(), name="room_create"),
    path("rooms/<int:pk>/edit/", views.RoomUpdateView.as_view(), name="room_edit"),
    path("rooms/<int:pk>/delete/", views.RoomDeleteView.as_view(), name="room_delete"),
    path("rooms/<int:pk>/start/", views.RoomStartView.as_view(), name="room_start"),
    path("rooms/<int:pk>/stop/", views.RoomStopView.as_view(), name="room_stop"),
    path("sessions/<int:pk>/log/", views.SessionLogView.as_view(), name="session_log"),
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("api/status/", views.status_api, name="status_api"),
    path("healthz", views.healthz, name="healthz"),
]
