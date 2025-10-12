"""Forms for manual backup and restore operations."""
from __future__ import annotations

from django import forms


class BackupForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Yedek almayı onaylıyorum",
        help_text="Bu işlem tüm veritabanlarını buluta yedekler.",
    )


class RestoreForm(forms.Form):
    database = forms.CharField(label="Veritabanı", max_length=128)
    date = forms.CharField(
        label="Yedek Tarihi",
        required=False,
        help_text="YYYY-AA-GG formatında girin. Boş bırakırsanız en güncel yedek kullanılır.",
    )
    skip_safety_backup = forms.BooleanField(
        label="Güvenli yedek oluşturmayı atla",
        required=False,
    )
