"""Validated profile configuration and karaoke metadata rules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import regex
from pydantic import BaseModel, Field, field_validator, model_validator


HEIGHTS = (144, 240, 360, 480, 720, 1080, 1440, 2160, 4320)
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SLUG = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


class ParseRule(BaseModel):
    pattern: str = Field(min_length=1, max_length=1024)
    artist: str = Field(max_length=256)
    title: str = Field(min_length=1, max_length=256)

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        regex.compile(value, regex.IGNORECASE)
        return value

    @model_validator(mode="after")
    def valid_templates(self) -> "ParseRule":
        names = set(regex.compile(self.pattern).groupindex)
        for template in (self.artist, self.title):
            for field in re.findall(r"\{([^{}]+)\}", template):
                if field not in names:
                    raise ValueError(f"unknown capture group: {field}")
            if template.count("{") != template.count("}"):
                raise ValueError("unbalanced template braces")
        return self


class Channel(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    language: str = Field(default="en", max_length=16)
    rules: list[ParseRule] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not CHANNEL_ID.fullmatch(value):
            raise ValueError("channel id must be a permanent YouTube UC identifier")
        return value


class Profile(BaseModel):
    slug: str
    name: str = Field(min_length=1, max_length=128)
    output_dir: str
    search_terms: list[str] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    min_height: int | None = None
    max_height: int | None = None
    quality_order: list[int] = Field(default_factory=list)
    format: Literal["mp4"] = "mp4"
    search_count: int = Field(default=15, ge=1, le=50)

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError("slug must contain lowercase letters, digits and hyphens")
        return value

    @field_validator("output_dir")
    @classmethod
    def valid_output(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\0" in value:
            raise ValueError("output_dir must be an absolute path")
        return value

    @field_validator("search_terms")
    @classmethod
    def valid_terms(cls, values: list[str]) -> list[str]:
        if len(values) > 10 or any(not term.strip() or len(term) > 80 for term in values):
            raise ValueError("search_terms must contain up to 10 nonempty terms")
        return [term.strip() for term in values]

    @model_validator(mode="after")
    def valid_quality(self) -> "Profile":
        if self.min_height is not None and self.min_height not in HEIGHTS:
            raise ValueError("unsupported minimum resolution")
        if self.max_height is not None and self.max_height not in HEIGHTS:
            raise ValueError("unsupported maximum resolution")
        if self.min_height and self.max_height and self.min_height > self.max_height:
            raise ValueError("minimum resolution exceeds maximum")
        allowed = self.allowed_heights()
        if not self.quality_order:
            self.quality_order = list(reversed(allowed))
        if len(self.quality_order) != len(allowed) or set(self.quality_order) != set(allowed):
            raise ValueError("quality_order must contain every resolution between the limits once")
        ids = [channel.id for channel in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate channel id")
        return self

    def allowed_heights(self) -> list[int]:
        return [height for height in HEIGHTS
                if (self.min_height is None or height >= self.min_height)
                and (self.max_height is None or height <= self.max_height)]

    def with_quality_limits(self, minimum: int | None, maximum: int | None) -> "Profile":
        data = self.model_dump()
        data["min_height"] = minimum
        data["max_height"] = maximum
        data["quality_order"] = []
        return Profile.model_validate(data)


def search_query(query: str, terms: list[str]) -> str:
    words = query.strip().split()
    if not words:
        raise ValueError("query is blank")
    result = " ".join(words)
    for term in terms:
        if term.casefold() not in result.casefold():
            result += " " + term
    return result


def _clean_title(value: str) -> str:
    value = re.sub(r"\s*[\(\[][^)\]]*(?:karaoke|instrumental|lyrics|letra)[^)\]]*[\)\]]", "", value, flags=re.I)
    value = re.sub(r"\s*[-|:]\s*(?:karaoke|versi[oó]n karaoke|karaoke version)(?:\s+.*)?$", "", value, flags=re.I)
    return " ".join(value.split())


def parse_metadata(video_title: str, channel: Channel | None) -> tuple[str, str]:
    """Apply channel rules first, then conservative karaoke fallbacks."""
    title = video_title.strip()
    if channel:
        for rule in channel.rules:
            try:
                match = regex.search(rule.pattern, title, regex.IGNORECASE, timeout=0.05)
            except TimeoutError:
                continue
            if match:
                fields = {name: value or "" for name, value in match.groupdict().items()}
                artist = _clean_title(rule.artist.format_map(fields))
                song = _clean_title(rule.title.format_map(fields))
                if song:
                    return artist or channel.name, song
    cleaned = _clean_title(title)
    if " - " in cleaned:
        artist, song = cleaned.split(" - ", 1)
        if artist.strip() and song.strip():
            return artist.strip(), song.strip()
    return (channel.name if channel else ""), cleaned or title


def default_profile(output_dir: str) -> Profile:
    common = ParseRule(pattern=r"^(?P<artist>.+?)\s+-\s+(?P<title>.+?)(?:\s*[\(\[].*)?$", artist="{artist}", title="{title}")
    flipped = ParseRule(pattern=r"^(?P<title>.+?)\s+-\s+(?P<artist>.+?)\s*\|\s*Karaoke.*$", artist="{artist}", title="{title}")
    media_flipped = ParseRule(pattern=r"^(?P<title>.+?)\s+-\s+(?P<artist>.+?)\s+-\s+(?:LETRA|LYRICS|KARAOKE)\b.*$", artist="{artist}", title="{title}")
    return Profile(
        slug="karaoke", name="YouTube Karaoke", output_dir=output_dir,
        search_terms=["karaoke"], min_height=480, max_height=1080,
        channels=[
            Channel(id="UCbqcG1rdt9LMwOJN4PyGTKg", name="KaraFun Karaoke", rules=[flipped, common]),
            Channel(id="UCwTRjvjVge51X-ILJ4i22ew", name="Sing King Karaoke", rules=[common]),
            Channel(id="UCrk8mp-ugqtAbjif6JARjlw", name="Zoom Karaoke Official", rules=[common]),
            Channel(id="UCWLqO9ztz16a_Ko4YB9PnFQ", name="Party Tyme Karaoke", rules=[common]),
            Channel(id="UCgJdYYa_ikkx_2eU5pNU3nw", name="Party Tyme Karaoke en Español", language="es", rules=[common]),
            Channel(id="UCmV_ZCnAAj5DbkUqpBC_CDg", name="KaraokeMedia", language="es", rules=[media_flipped, common]),
            Channel(id="UCu2boX8Y663cxXFe4dK4U6w", name="Karaoke Total Profesional", language="es", rules=[
                ParseRule(pattern=r"^(?P<title>.+?)(?:\s+[-–—]\s*|[-–—]\s+)(?P<artist>.+?)\s*(?:[-–—]\s*)?\(?\bKaraoke\b.*$",
                          artist="{artist}", title="{title}"),
            ]),
            Channel(id="UC5Zv8wTsCk3gVJcBcHmOPgg", name="KaraFun Karaoke - España", language="es", rules=[
                ParseRule(pattern=r"^Karaoke\s+(?P<title>.+?)\s+[-–—]\s+(?P<artist>.+?)(?:\s*\*+)?$",
                          artist="{artist}", title="{title}"),
            ]),
            Channel(id="UCYi9TC1HC_U2kaRAK6I4FSQ", name="Stingray Karaoke", rules=[
                ParseRule(pattern=r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*\(Karaoke Version\)$",
                          artist="{artist}", title="{title}"),
            ]),
            Channel(id="UCkQmmoZPHkgj2J5YxyoxRRA", name="Kar@ke", language="es", rules=[
                ParseRule(pattern=r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*\(Kar@ke\)$",
                          artist="{artist}", title="{title}"),
            ]),
        ],
    )
