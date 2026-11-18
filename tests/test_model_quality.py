from pathlib import Path

import pytest
from pydantic import ValidationError

from youtube_provider.model import Channel, ParseRule, Profile, default_profile, parse_metadata, search_query
from youtube_provider.quality import QualityUnavailable, select_format


def profile(tmp_path: Path) -> Profile:
    return default_profile(str(tmp_path / "downloads"))


def test_default_profile_has_strict_curated_channels(tmp_path):
    configured = profile(tmp_path)
    assert configured.slug == "karaoke"
    assert len(configured.channels) == 6
    assert len({channel.id for channel in configured.channels}) == 6
    assert configured.quality_order == [1080, 720, 480]


def test_channel_rules_override_generic_artist_title(tmp_path):
    configured = profile(tmp_path)
    karafun = configured.channels[0]
    assert parse_metadata("Can't Take My Eyes Off You - Frankie Valli | Karaoke Version | KaraFun", karafun) == (
        "Frankie Valli", "Can't Take My Eyes Off You")
    assert parse_metadata("ABBA - Dancing Queen (Karaoke Version)", configured.channels[1]) == (
        "ABBA", "Dancing Queen")
    assert parse_metadata("La Flaca - Jarabe De Palo - LETRA (INSTRUMENTAL KARAOKE)", configured.channels[-1]) == (
        "Jarabe De Palo", "La Flaca")


def test_rule_validation_prevents_missing_capture_and_bad_channel(tmp_path):
    with pytest.raises(ValidationError):
        ParseRule(pattern="(?P<title>.*)", artist="{missing}", title="{title}")
    with pytest.raises(ValidationError):
        Channel(id="@name", name="Impersonator")


def test_search_terms_are_added_only_once():
    assert search_query("  ABBA   karaoke ", ["karaoke"]) == "ABBA karaoke"
    assert search_query("ABBA", ["karaoke"]) == "ABBA karaoke"


def test_quality_preference_can_choose_downscale_and_never_upscales(tmp_path):
    configured = profile(tmp_path)
    configured.quality_order = [720, 1080, 480]
    formats = [
        {"format_id": "video1080", "height": 1080, "vcodec": "avc1.1", "acodec": "none", "ext": "mp4"},
        {"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
    ]
    selected = select_format(formats, configured)
    assert (selected.source_height, selected.target_height, selected.transcode) == (1080, 720, True)
    assert selected.format_selector == "video1080+audio"

    too_small = [{**formats[0], "height": 360}, formats[1]]
    with pytest.raises(QualityUnavailable, match="minimum"):
        select_format(too_small, configured)


def test_quality_order_must_be_complete(tmp_path):
    data = profile(tmp_path).model_dump()
    data["quality_order"] = [1080, 720]
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
