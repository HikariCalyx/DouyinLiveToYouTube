"""Forms used by the dashboard."""

import re

from django import forms

from .models import GlobalSettings, Room


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "douyin_url", "quality", "stream_key", "ingest_url", "enabled"]
        widgets = {
            "douyin_url": forms.TextInput(attrs={"placeholder": "https://live.douyin.com/123456789"}),
            "ingest_url": forms.TextInput(attrs={"placeholder": "rtmp://a.rtmp.youtube.com/live2/xxxx"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Entering "live.douyin.com/123" should become https://, not http://
        self.fields["douyin_url"].assume_scheme = "https"

    def clean_douyin_url(self) -> str:
        url = (self.cleaned_data["douyin_url"] or "").strip()
        if url and "://" not in url:
            url = "https://" + url
        if not re.match(r"^https?://", url):
            raise forms.ValidationError("仅支持 http/https 地址")
        if "douyin.com" not in url:
            raise forms.ValidationError("请输入抖音直播间地址（需包含 douyin.com）")
        return url

    def clean_ingest_url(self) -> str:
        value = (self.cleaned_data["ingest_url"] or "").strip()
        if value and not value.startswith(("rtmp://", "rtmps://")):
            raise forms.ValidationError("完整推流地址必须以 rtmp:// 或 rtmps:// 开头")
        return value


class SettingsForm(forms.ModelForm):
    class Meta:
        model = GlobalSettings
        exclude = ["updated_at"]
        widgets = {
            "cookie": forms.Textarea(attrs={"rows": 4, "spellcheck": "false"}),
        }

    def clean_poll_interval(self) -> int:
        value = self.cleaned_data["poll_interval"]
        if value < 5:
            raise forms.ValidationError("检测间隔不能小于 5 秒")
        return value

    def clean_retry_interval(self) -> int:
        value = self.cleaned_data["retry_interval"]
        if value < 1:
            raise forms.ValidationError("断流重试间隔不能小于 1 秒")
        return value

    def clean_video_bitrate_kbps(self) -> int:
        value = self.cleaned_data["video_bitrate_kbps"]
        if value < 500:
            raise forms.ValidationError("视频码率不能小于 500 kbps")
        return value
