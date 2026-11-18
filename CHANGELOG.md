# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- A failed channel search no longer discards matches from other channels, and a top-level YouTube failure no longer appears as an empty successful search.
- Download workers keep yt-dlp and child-process diagnostics out of the JSON event stream, preventing spurious download failures.
- Admin login and configuration requests work through local port forwarding that changes the internal request origin, while cross-site browser requests remain blocked.

### Changed

- Restricted searches now query each YouTube channel directly and filter/rank results against the requested artist and title, improving recall and removing unrelated partial matches.
- Search results follow the configured channel order, with move-up and move-down controls in administration.
- New downloads use clean artist and title filenames without YouTube IDs. Colliding names receive a numbered suffix.
- Client API keys can use their own output directory; by default they inherit the profile directory. Existing databases are migrated automatically.

### Added

- Administration folder picker for profile and client output directories, showing folders visible and writable by the provider while retaining manual path entry.
- Initial project documentation and ignore rules.
- Standalone Python SongProvider v0 service with authenticated search, per-client downloads, status streams, and SQLite persistence.
- Editable karaoke profile with six curated channels, channel-specific artist/title rules, quality preferences, and MP4 output.
- Local administration interface for profiles, API keys, parsing previews, password changes, and manual or scheduled yt-dlp updates.
- Docker Compose deployment for x86-64 and ARM64 with separate state and media volumes, automated tests, and ISC licensing.
