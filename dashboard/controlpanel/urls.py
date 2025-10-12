"""URL routing for control panel app."""
from django.urls import path

from . import views

app_name = "controlpanel"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("backups/run/", views.TriggerBackupView.as_view(), name="trigger-backup"),
    path("restores/run/", views.TriggerRestoreView.as_view(), name="trigger-restore"),
    path("cron/status/", views.CronStatusView.as_view(), name="cron-status"),
]
