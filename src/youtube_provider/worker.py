"""One-shot yt-dlp worker. JSON requests on stdin, JSON events on stdout."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlencode

import yt_dlp

from .model import Channel, Profile, parse_metadata, search_query
from .quality import select_format


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_protocol_output: TextIO | None = None


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")),
          file=_protocol_output or sys.stdout, flush=True)


def isolate_protocol_output() -> None:
    """Reserve stdout for JSON events and route library/process output to stderr."""
    global _protocol_output
    _protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr


def _ydl_options(**extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "js_runtimes": {"node": {"path": shutil.which("node")}} if shutil.which("node") else {"deno": {}},
    }
    options.update(extra)
    return options


def _video_info(ydl: yt_dlp.YoutubeDL, video_id: str) -> dict:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("invalid YouTube video id")
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    if not isinstance(info, dict) or info.get("id") != video_id:
        raise ValueError("video metadata unavailable")
    return info


def _channel(profile: Profile, channel_id: str | None) -> Channel | None:
    if not profile.channels:
        return None
    return next((channel for channel in profile.channels if channel.id == channel_id), None)


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("’", "").replace("'", "").replace("&", " and ")
    return [word for word in re.split(r"[^\w]+", normalized.replace("_", " ")) if word]


def _contains(words: list[str], phrase: list[str]) -> bool:
    return any(words[index:index + len(phrase)] == phrase
               for index in range(len(words) - len(phrase) + 1))


def relevance(query: str, artist: str, title: str, search_terms: list[str]) -> int:
    """Require whole query words in parsed metadata, then rank closer matches."""
    requested = _words(query)
    for term in search_terms:
        phrase = _words(term)
        if phrase and _contains(requested, phrase):
            start = next(index for index in range(len(requested) - len(phrase) + 1)
                         if requested[index:index + len(phrase)] == phrase)
            del requested[start:start + len(phrase)]
    if not requested:
        return 1
    artist_words, title_words = _words(artist), _words(title)
    combined = artist_words + title_words
    if not set(requested).issubset(combined):
        return 0
    if requested == title_words:
        return 5
    if requested in (combined, title_words + artist_words):
        return 4
    if _contains(title_words, requested):
        return 3
    if _contains(combined, requested) or _contains(title_words + artist_words, requested):
        return 2
    return 1


def search(request: dict) -> None:
    profile = Profile.model_validate(request["profile"])
    query = str(request["query"]).strip()
    requested_channel = request.get("channel_id")
    channel = _channel(profile, requested_channel) if requested_channel else None
    if requested_channel and not channel:
        raise ValueError("unknown channel")
    direct_id = request.get("video_id")
    results: list[tuple[int, dict]] = []
    candidate_count = min(100, profile.search_count * 3)
    with yt_dlp.YoutubeDL(_ydl_options(extract_flat="in_playlist", ignoreerrors=True,
                                       skip_download=True, playlistend=candidate_count)) as ydl:
        if direct_id:
            entries = [_video_info(ydl, str(direct_id))]
        else:
            terms = search_query(query, profile.search_terms)
            if channel:
                source = f"https://www.youtube.com/channel/{channel.id}/search?{urlencode({'query': terms})}"
            else:
                source = f"ytsearch{candidate_count}:{terms}"
            listing = ydl.extract_info(source, download=False)
            if not isinstance(listing, dict):
                raise RuntimeError("YouTube search is unavailable")
            entries = list(listing.get("entries") or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "")
            if not VIDEO_ID.fullmatch(video_id):
                continue
            actual_id = entry.get("channel_id")
            if profile.channels and (not actual_id or not _channel(profile, actual_id)):
                # Flat search data can omit the permanent channel id. Hydrate it;
                # never trust a display name to enforce the allowlist.
                try:
                    entry = _video_info(ydl, video_id)
                except Exception:
                    continue
                actual_id = entry.get("channel_id")
            if profile.channels and not _channel(profile, actual_id):
                continue
            if channel and actual_id != channel.id:
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            matched_channel = _channel(profile, actual_id)
            artist, song = parse_metadata(title, matched_channel)
            score = 1 if direct_id else relevance(query, artist, song, profile.search_terms)
            if not score:
                continue
            duration = entry.get("duration")
            result = {
                "id": video_id, "title": song[:1024], "artist": artist[:1024],
                "source": (matched_channel.name if matched_channel else str(entry.get("channel") or "YouTube"))[:1024],
                "channel_id": actual_id, "raw_title": title[:1024],
            }
            if isinstance(duration, (int, float)) and duration > 0:
                result["duration"] = float(duration)
            results.append((score, result))
    results.sort(key=lambda pair: -pair[0])
    emit({"event": "results", "items": [item for _, item in results[:profile.search_count]]})


def download(request: dict) -> None:
    profile = Profile.model_validate(request["profile"])
    video_id = str(request["video_id"])
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("invalid YouTube video id")
    final_path = Path(request["final_path"]).resolve()
    stage = Path(request["stage_dir"]).resolve()
    output_root = Path(profile.output_dir).resolve()
    if not final_path.is_relative_to(output_root) or not stage.is_relative_to(output_root):
        raise ValueError("output path escapes profile directory")
    stage.mkdir(parents=True, exist_ok=True)
    try:
        with yt_dlp.YoutubeDL(_ydl_options(skip_download=True)) as probe:
            info = _video_info(probe, video_id)
        channel_id = info.get("channel_id")
        if profile.channels and not _channel(profile, channel_id):
            raise ValueError("video channel is not allowed by this profile")
        selected = select_format(info.get("formats") or [], profile)
        emit({"event": "selected", "source_height": selected.source_height,
              "target_height": selected.target_height})

        def progress(data: dict) -> None:
            if data.get("status") != "downloading":
                return
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            fraction = min(0.9, 0.9 * downloaded / total) if total else 0.0
            eta = data.get("eta")
            emit({"event": "progress", "progress": fraction,
                  "eta": eta if isinstance(eta, (int, float)) and eta >= 0 else None})

        options = _ydl_options(
            format=selected.format_selector,
            outtmpl=str(stage / "media.%(ext)s"),
            merge_output_format="mkv" if selected.transcode else "mp4",
            progress_hooks=[progress],
            nopart=False,
        )
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        candidates = [item for item in stage.glob("media.*") if item.is_file() and not item.name.endswith((".part", ".ytdl"))]
        if len(candidates) != 1:
            raise RuntimeError("yt-dlp did not produce exactly one media file")
        source = candidates[0]
        emit({"event": "progress", "progress": 0.92})
        processed = stage / "finished.mp4"
        command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source)]
        if selected.transcode:
            command += ["-vf", f"scale=-2:{selected.target_height}",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac"]
        else:
            command += ["-c", "copy"]
        command += ["-movflags", "+faststart", str(processed)]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3600)
        if not processed.is_file() or processed.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced no media file")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(processed, final_path)
        emit({"event": "complete", "path": str(final_path)})
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main() -> None:
    isolate_protocol_output()
    try:
        request = json.loads(sys.stdin.readline())
        if request.get("action") == "search":
            search(request)
        elif request.get("action") == "download":
            download(request)
        else:
            raise ValueError("unknown worker action")
    except Exception as exc:
        emit({"event": "error", "message": str(exc)[:1024]})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
