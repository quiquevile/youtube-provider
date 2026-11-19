# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- First-time admin password setup and sign-in work when the browser opens the provider by its LAN address over HTTP.
- A failed channel search no longer discards matches from other channels, and a top-level YouTube failure no longer appears as an empty successful search.
- Download workers keep yt-dlp and child-process diagnostics out of the JSON event stream, preventing spurious download failures.
- Admin login and configuration requests work through local port forwarding that changes the internal request origin, while cross-site browser requests remain blocked.

### Changed

- New channel parsing rules start with the preset's artist–title pattern and both output templates already filled in.
- GitHub Actions now uses Node.js 24 actions for validation, Docker builds, and image publishing.
- Docker Compose uses `PUID` and `PGID` to run the provider and create its database and downloads with the configured ownership. Both IDs default to 10001.
- README now guides users from Docker or local installation through connecting Karaoke Eternal, sharing the download folder, and downloading their first song.
- The runtime container now uses Alpine with Python, FFmpeg, and only the Node.js executable. Docker build stages cache Python dependencies before copying application code, and the build context excludes local files.
- Python packaging now requires Pydantic 2 explicitly, including on ARM64 builds.
- README now states that Karaoke Eternal karaoke integration is the project's current supported use case and explains its shared-media requirement.
- Restricted searches now query each YouTube channel directly and filter/rank results against the requested artist and title, improving recall and removing unrelated partial matches.
- Search results follow the configured channel order, with move-up and move-down controls in administration.
- New downloads use clean artist and title filenames without YouTube IDs. Colliding names receive a numbered suffix.
- Client API keys can use their own output directory; by default they inherit the profile directory. Existing databases are migrated automatically.

### Added

- The provider publishes its icon to a shared directory on startup. Karaoke Eternal can serve it from its existing port, with a new image URL when the icon content changes.
- New karaoke profiles include Karaoke Total Profesional, KaraFun Karaoke - España, Stingray Karaoke, and Kar@ke with channel-specific artist and title rules.
- On first launch, the administration page lets you create the admin password and signs you in immediately.
- CI now smoke-tests the built image on each native architecture, including an H.264/AAC FFmpeg conversion.
- GitHub Actions validation and native AMD64/ARM64 Docker Hub publishing for development, release, main, and version tags.
- Administration folder picker for profile and client output directories, showing folders visible and writable by the provider while retaining manual path entry.
- Initial project documentation and ignore rules.
- Standalone Python SongProvider v0 service with authenticated search, per-client downloads, status streams, and SQLite persistence.
- Editable karaoke profile with six curated channels, channel-specific artist/title rules, quality preferences, and MP4 output.
- Local administration interface for profiles, API keys, parsing previews, password changes, and manual or scheduled yt-dlp updates.
- Docker Compose deployment for x86-64 and ARM64 with separate state and media volumes, automated tests, and ISC licensing.

### Security

- The Docker image requires Alpine's patched OpenSSL libraries (3.5.9-r1 or later) for CVE-2026-88823, CVE-2026-95691, CVE-2026-95692, and CVE-2026-107496.
- Removed npm and its unused dependency tree from the runtime image; Alpine system packages and the pip used by yt-dlp updates are upgraded during the build.
