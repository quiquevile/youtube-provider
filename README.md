# YouTube Song Provider

A standalone, self-hosted YouTube provider for the [Karaoke Eternal SongProvider v0 API](https://github.com/bhj/reference-provider-song). The HTTP API serves one or more independently configured profiles; the included `karaoke` profile is ready to edit in the local administration interface. The project is licensed under [ISC](LICENSE).

## Features

- SongProvider v0 discovery, bearer authentication, NDJSON search, fetch/cancel, and SSE download status.
- A curated karaoke profile with KaraFun, Sing King, Zoom Karaoke, Party Tyme, Party Tyme en Español, and KaraokeMedia. Each channel has a permanent YouTube ID and editable artist/title parsing rules. An empty channel list allows any channel; a nonempty list is enforced again before download.
- Per-profile search terms, result count, output directory, resolution limits, and reorderable quality preferences. Karaoke output is MP4. A source below the minimum is rejected; a source above the chosen target is scaled down, never upscaled.
- Multiple revocable API keys per profile, each identifying an isolated consumer with its own download state and optional output directory.
- A separate local administration listener for profiles, parsing previews, API keys, password changes, and manual or scheduled yt-dlp updates (`stable` or `nightly`).
- SQLite persistence and Docker support for `amd64` and `arm64`.

## Docker quick start

The image includes Python, Node.js (used by yt-dlp for YouTube's JavaScript), and FFmpeg. [Docker Compose](compose.yaml) publishes both listeners on the host's loopback address only.

```sh
docker compose build
docker compose run --rm provider youtube-song-provider admin set-password
docker compose up -d
```

Open `http://127.0.0.1:3031` to sign in. Create a key for the `karaoke` profile and copy it immediately; only its hash is stored. Configure the consumer with the base URL `http://127.0.0.1:3030/profiles/karaoke` and the new key. When the consumer runs in another container, use an address reachable from that container, such as the provider's name on a shared Docker network.

Compose keeps SQLite and update packages in the `youtube-provider-state` volume. Finished files go to the separate `youtube-provider-media` volume, initially under `/media/karaoke/`. A consumer that ingests files must mount **only the media volume at the same absolute path** (`/media`) and configure that path as a library folder. Do not mount the state volume into consumers because it holds credential hashes and administrative sessions.

For a remote consumer, publish the API behind HTTPS. Configure `YSP_PUBLIC_BASE_URL` with the externally reachable origin, such as `https://songs.example.com`, so the source icon URL works in end-user browsers. Disable reverse-proxy buffering for search and status streams. The administration port is intended to stay local; exposing it through a separately secured reverse proxy is an operator decision.

## Local development

Requires Python 3.12+, Node.js, and `ffmpeg`/`ffprobe` on `PATH`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/youtube-song-provider admin set-password
.venv/bin/youtube-song-provider serve
```

The API listens on `127.0.0.1:3030` and administration on `127.0.0.1:3031`. Data and local downloads default to `./data`. Run `youtube-song-provider --help` for command options.

When opening administration through a local port forward or HTTPS proxy, preserve the browser's `Sec-Fetch-Site` header. It lets same-origin login and configuration requests pass even if the proxy changes the internal host or scheme; cross-site requests remain blocked.

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `YSP_DATA_DIR` | SQLite database and managed yt-dlp packages | `./data` |
| `YSP_OUTPUT_ROOT` | Output root used when the first profile is created | `<data-dir>/downloads` |
| `YSP_API_HOST`, `YSP_API_PORT` | SongProvider listener | `127.0.0.1`, `3030` |
| `YSP_ADMIN_HOST`, `YSP_ADMIN_PORT` | Administration listener | `127.0.0.1`, `3031` |
| `YSP_PUBLIC_BASE_URL` | Browser-visible API origin for icons | Derived from the request |
| `YSP_ADMIN_COOKIE_SECURE` | Set to `1` when administration is served through HTTPS | `0` |
| `YSP_CONCURRENCY` | Maximum simultaneous downloads | `2` |

## Profile configuration

The `karaoke` profile starts with the six curated channels and `karaoke` appended to searches. Channel entries can be added, removed, edited, or moved up and down in administration. Their saved order sets search result priority: all results from a higher channel are streamed before those from a lower channel, even if the lower channel finishes searching first. Duplicate video IDs keep the result from the highest-priority channel. Use the permanent `UC...` channel ID, not a handle or display name. Channel restrictions apply to search results, pasted YouTube URLs, and downloads.

For a restricted profile, each configured channel is searched within that channel rather than by adding its name to a global YouTube query. The provider checks the permanent channel ID on every candidate and returns only videos whose parsed artist and title contain every whole word from the user's query (case and accents ignored). Configured default search terms such as `karaoke` are excluded from this relevance check. Exact title matches rank first within a channel. The results-per-channel setting limits returned matches; the provider scans a larger bounded set of candidates to avoid losing relevant videos among YouTube's approximate matches. A pasted YouTube video URL bypasses text matching but still obeys the channel allowlist.

If one channel search fails, matches from other channels are still returned. The search reports an error when all configured channels fail.

Profiles can be cloned and deleted from administration. At least one profile must remain so the interface always has a configuration to clone.

Each channel has ordered parsing rules. A rule is a regular expression with named capture groups and templates for `artist` and `title`. For example, `(?P<artist>.+) - (?P<title>.+)` with `{artist}` and `{title}`. Rules are tried in order for the matching channel; a conservative fallback is used when none matches. The preview panel tests unsaved rules with a sample title. Rules are evaluated with a timeout.

Minimum and maximum resolution use preset dropdowns (`144p` through `4320p`, or no limit). The interface generates the resolutions between those limits and lets you drag them into preference order. A selected source is remuxed to MP4 when its codecs are compatible, or transcoded with FFmpeg when scaling or codec conversion is needed. Profile edits apply to new jobs; running jobs retain their original configuration.

Each client key can have its own absolute output directory, set when creating the key or edited later. Leave it blank to use the profile's directory. In Docker, custom directories should be writable by the provider and mounted persistently. New files use clean names such as `Elton John - Your Song.mp4`; when a name is already used, the provider reserves `Elton John - Your Song (2).mp4` and so on. Existing files are not renamed or moved. A completed item requested again by the same key keeps its existing path; folder changes apply to new downloads.

The administration page has a **Browse…** button beside each output directory field. It opens a folder picker for directories visible to the provider process and lets you select an existing writable folder. You can still type any absolute path manually, including a directory that will be created later. A web browser cannot supply a usable absolute path from its native operating-system folder dialog; in Docker, the provider's paths are inside the container rather than on the browser's host.

## SongProvider API

For a profile named `karaoke`, the provider base URL is `/profiles/karaoke`. Append these paths to that base URL:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api` | Unauthenticated discovery |
| `GET` | `/api/auth/v0` | Check a bearer key |
| `POST` | `/api/song/v0/search` | Stream NDJSON result arrays for `{"query":"..."}` |
| `POST` | `/api/song/v0/items/{id}/fetch` | Accept `{"timestamp":123,"meta":{...}}` and start or reconcile a download |
| `POST` | `/api/song/v0/items/{id}/cancel` | Cancel this client's download |
| `GET` | `/api/song/v0/status?since=123` | SSE snapshot and live updates for this client |

Every endpoint except discovery requires `Authorization: Bearer <key>`. Search item IDs are stable YouTube video IDs. Download state is isolated by key. The `complete` event is emitted only after the MP4 has been moved to its final path.

If a previous installation reports `invalid worker event` during a download, update and restart the provider, then retry the item. The provider cleans up incomplete staging files after a failed job.

Search is limited to 10 requests per minute per key and fetch to 20 requests per minute per key. Up to four status streams may be open per key. These limits protect a self-hosted instance from accidental overload.

## Updates and tests

yt-dlp updates are installed in versioned directories under the state volume. The admin can update immediately or choose `stable`/`nightly` and an interval in hours (`0` disables automatic updates). A successful installation is verified before new jobs switch to it; running workers finish on their previous version.

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m coverage run -m pytest -q
.venv/bin/python -m coverage report -m
```

The integration test opens temporary loopback ports to verify that the administration and SongProvider listeners stay separate.
