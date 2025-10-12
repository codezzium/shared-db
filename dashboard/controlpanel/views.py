"""View layer for control panel."""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from . import forms, services


class DashboardView(View):
    template_name = "controlpanel/dashboard.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        context = services.build_dashboard_context()
        context["backup_form"] = forms.BackupForm()
        context["restore_form"] = forms.RestoreForm()
        return render(request, self.template_name, context)


class TriggerBackupView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        form = forms.BackupForm(request.POST)
        if not form.is_valid():
            for error in form.errors.values():
                messages.error(request, error.as_text())
            return redirect(reverse("controlpanel:dashboard"))

        success, details = services.run_backup()
        if success:
            messages.success(request, "Yedekleme başlatıldı.")
        else:
            messages.error(request, "Yedekleme başlatılamadı.")
        for line in details:
            messages.info(request, line)
        return redirect(reverse("controlpanel:dashboard"))


class TriggerRestoreView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        form = forms.RestoreForm(request.POST)
        if form.is_valid():
            success, details = services.run_restore(
                db=form.cleaned_data["database"],
                date=form.cleaned_data["date"],
                skip_safety=form.cleaned_data["skip_safety_backup"],
            )
            if success:
                messages.success(request, "Geri yükleme işlemi başlatıldı.")
            else:
                messages.error(request, "Geri yükleme sırasında hata oluştu.")
            for line in details:
                messages.info(request, line)
        else:
            for error in form.errors.values():
                messages.error(request, error.as_text())
        return redirect(reverse("controlpanel:dashboard"))


class CronStatusView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        for line in services.get_cron_status():
            messages.info(request, line)
        return redirect(reverse("controlpanel:dashboard"))
