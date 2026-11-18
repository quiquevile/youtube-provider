# YouTube Song Provider for Karaoke Eternal

Search YouTube from Karaoke Eternal and download karaoke songs as MP4 files. The included `karaoke` profile is ready to use.

## Install

Run one of the following from the repository directory.

**Docker Compose** (AMD64 or ARM64)

```sh
docker compose build
docker compose run --rm provider youtube-song-provider admin set-password
docker compose up -d
```

**Without Docker** (requires Python 3.12+, Node.js 22+, FFmpeg and FFprobe)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/youtube-song-provider admin set-password
.venv/bin/youtube-song-provider serve
```

## Connect Karaoke Eternal

1. Open [provider administration](http://127.0.0.1:3031), sign in and create an API key for the `karaoke` profile. Copy the key while it is visible.
2. In Karaoke Eternal, add a song provider with that key and the base URL `http://127.0.0.1:3030/profiles/karaoke`. If both services run in containers on a shared Docker network, use `http://provider:3030/profiles/karaoke`.
3. Add the provider's download folder to the Karaoke Eternal library. The default is `./data/downloads/karaoke` for a local installation or `/media/karaoke` with Compose. Both services must see the files at the **same absolute path**. With two containers, mount the existing `youtube-provider-media` volume at `/media` in Karaoke Eternal.
4. Search by artist and title in Karaoke Eternal and download a song. Its MP4 appears in the library folder.

In provider administration you can reorder allowed channels, set download quality and folders, and update yt-dlp manually or on a schedule.

[ISC license](LICENSE)
