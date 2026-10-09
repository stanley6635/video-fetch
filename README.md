# video-fetch

Download videos, image notes, and article images to local files — from **抖音 Douyin**, **小红书 Xiaohongshu**, **WeChat 公众号 articles**, and **authenticated SharePoint / enterprise sites**.

Platform free endpoints change often, and the naive approach usually fails silently (a stripped page, an empty JSON payload, a 403 that looks like a ban). This skill encodes the currently validated request paths as ready-to-run scripts, plus the environment gotchas that make them work.

## Scope and ethics

- **Single link at a time.** One video, one image note, one article. This is not a bulk scraper — batch-collecting these platforms risks account bans.
- **Only download what you are entitled to access.** Your own content, or internal/authorized material. Everything else is on you.
- **Downloads only.** No transcription, no OCR, no republishing.

## Requirements

- Python 3.10+ (scripts are stdlib + `curl` unless noted)
- `curl` on `PATH`
- [`ffprobe`](https://ffmpeg.org/) for verification (optional but recommended)
- For Douyin: [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and Playwright (`uv run --with playwright==1.63.0` provisions it on the fly)
- For SharePoint: Playwright plus a system Google Chrome (the script opens a real window so you can sign in)

The scripts are written for macOS and fall back to `/opt/homebrew/bin/` when `ffprobe`/`yt-dlp` are not found on `PATH`. Other platforms mostly work — adjust the binary lookup if needed.

## Usage

```bash
# 小红书 — video note, image note, or metadata only
python3 scripts/fetch_xhs.py "<share text / xhslink.cn link / note URL>" -o ~/Downloads
python3 scripts/fetch_xhs.py "<note URL>" --info          # preview before a 285 MB podcast
python3 scripts/fetch_xhs.py "<note URL>" --codec h265    # same resolution, smaller file

# 抖音
uv run --with playwright==1.63.0 python scripts/fetch_douyin.py "<share text / v.douyin.com link / video id>"

# 公众号 — video (best quality) + all body images
python3 scripts/fetch_wechat.py "https://mp.weixin.qq.com/s/..." -o ~/Downloads
python3 scripts/fetch_wechat.py "<url>" --info            # title, quality variants, image count
python3 scripts/fetch_wechat.py "<url>" --video-only

# SharePoint / authenticated enterprise pages
uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" --list
uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" --title "Quarterly All Hands"
```

Default output directory is `~/Downloads`; pass `-o` to override. Every download is verified with `ffprobe` (video) or a file-size floor (images).

### SharePoint sign-in

SharePoint pages sit behind the viewer's own login, so `fetch_sharepoint.py` is **not headless**: it opens a real Chrome window and waits for you to sign in (SSO + MFA), then reuses that profile on later runs (`~/Library/Caches/sp-fetch/chrome-profile`). The exported cookie jar is a temporary file and is deleted right after the download — only the profile directory is kept.

## How it works

| Platform | Path |
|---|---|
| 小红书 | short link → `window.__INITIAL_STATE__` → pre-signed `masterUrl` (video) or `imageList` (images); mobile UA required; note ID is verified against the link |
| 抖音 | short link → headless Chromium exports fresh cookies → `yt-dlp` downloads and merges (≤720p, no watermark) |
| 公众号 | article HTML → embedded quality-variant array (pick largest) + `content_noencode` body images on `qpic.cn`; anti-leech CDN needs a retry loop and a `Host` header override |
| SharePoint | rendered page HTML → `videosrc` attribute on the play button → `curl` with the session cookie jar |

Each platform's specific traps — stale tokens serving you the wrong note, HTTP 416 resume state, CDN edge redirects, dead endpoints — are documented in `SKILL.md` and encoded in the scripts. Read the comments before "simplifying" a retry loop away.

## Environment notes

- If a **dead local proxy** is configured (`http_proxy=http://127.0.0.1:*` with no listener), it hijacks all traffic. Every network call bypasses it: curl `--noproxy '*'`, Chromium `--no-proxy-server`, yt-dlp `--proxy ""`. The scripts strip all `*proxy*` env vars themselves.
- macOS TCC blocks reading Safari cookie files, so `--cookies-from-browser safari` is not an option.
- A sandboxed agent environment may block reading Chrome's own profile directory; the SharePoint script uses a throwaway profile for this reason.

## Adding a platform

Write `scripts/fetch_<platform>.py` following the same conventions: resolve short links first, bypass the proxy, verify with `ffprobe`, default to `~/Downloads`, keep it stdlib + `curl` where possible.

## License

MIT — see [LICENSE](LICENSE).
