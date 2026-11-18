"""Deterministic resolution and MP4 format selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import Profile


class QualityUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Selection:
    video_id: str
    audio_id: str | None
    source_height: int
    target_height: int
    transcode: bool

    @property
    def format_selector(self) -> str:
        return self.video_id + (f"+{self.audio_id}" if self.audio_id else "")


def _video(format_: dict[str, Any]) -> bool:
    return format_.get("vcodec") not in (None, "none") and isinstance(format_.get("height"), (int, float))


def _audio(format_: dict[str, Any]) -> bool:
    return format_.get("acodec") not in (None, "none")


def _h264(codec: str) -> bool:
    return codec.startswith(("avc1", "h264"))


def _aac(codec: str) -> bool:
    return codec.startswith(("mp4a", "aac"))


def select_format(formats: list[dict[str, Any]], profile: Profile) -> Selection:
    videos = [fmt for fmt in formats if _video(fmt) and fmt.get("format_id")]
    if profile.min_height is not None:
        videos = [fmt for fmt in videos if fmt["height"] >= profile.min_height]
    if not videos:
        raise QualityUnavailable("no source video meets the minimum resolution")

    audio_only = [fmt for fmt in formats if _audio(fmt) and fmt.get("vcodec") == "none" and fmt.get("format_id")]
    audio_only.sort(key=lambda fmt: (
        not _aac(str(fmt.get("acodec", ""))),
        -(fmt.get("abr") or fmt.get("tbr") or 0),
    ))

    for target in profile.quality_order:
        candidates = [fmt for fmt in videos if fmt["height"] >= target]
        if not candidates:
            continue  # never upscale a lower-resolution source
        candidates.sort(key=lambda fmt: (
            fmt["height"] != target,
            fmt["height"],
            not _h264(str(fmt.get("vcodec", ""))),
            fmt.get("ext") != "mp4",
            -float(fmt.get("tbr") or 0),
        ))
        video = candidates[0]
        has_audio = _audio(video)
        audio = None if has_audio else (audio_only[0] if audio_only else None)
        if not has_audio and audio is None:
            continue
        transcode = (video["height"] != target
                     or not _h264(str(video.get("vcodec", "")))
                     or not _aac(str((video if has_audio else audio).get("acodec", ""))))
        return Selection(str(video["format_id"]), str(audio["format_id"]) if audio else None,
                         int(video["height"]), target, transcode)
    raise QualityUnavailable("no source video matches the configured resolution preferences")
