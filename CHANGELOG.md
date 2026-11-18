# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Admin login and configuration requests work through local port forwarding that changes the internal request origin, while cross-site browser requests remain blocked.

### Added

- Initial project documentation and ignore rules.
- Standalone Python SongProvider v0 service with authenticated search, per-client downloads, status streams, and SQLite persistence.
- Editable karaoke profile with six curated channels, channel-specific artist/title rules, quality preferences, and MP4 output.
- Local administration interface for profiles, API keys, parsing previews, password changes, and manual or scheduled yt-dlp updates.
- Docker Compose deployment for x86-64 and ARM64 with separate state and media volumes, automated tests, and ISC licensing.
