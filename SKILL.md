---
name: video-fetch
description: Use when the user asks to 下载视频 / 保存视频 / 存图 / 抓取配图 / 下载公众号文章媒体 / download or save a video, image note, or article media from Douyin 抖音, Xiaohongshu 小红书, or a WeChat official-account (公众号) article, pastes a share text or link from these platforms (xhslink.cn, v.douyin.com, xiaohongshu.com/discovery/item, douyin.com/video, mp.weixin.qq.com/s/...), or asks whether such media can be saved locally. Download only; transcription/逐字稿 and OCR are not covered here.
---

# Video Fetch

Download videos (and image notes / article images) from Douyin (抖音), Xiaohongshu (小红书), WeChat official-account (公众号) articles, and authenticated SharePoint/enterprise sites to local files.

## Overview

Platform free endpoints change often, so this skill encodes the **currently validated paths** (Douyin/XHS 2026-10-01, WeChat 2026-10-08) as ready-to-run scripts, plus the environment gotchas that make naive attempts fail. Run scripts, don't improvise; if a script fails, fall back to the manual procedures below before concluding the platform changed.

This is a **system-level** skill: it serves any project, not one repo. Downloads go to `~/Downloads` by default.

**Boundary: single-link downloads only — one video, one image note, or one article. Never batch-collect or bulk-scrape these platforms; that risks bans.** Only download material the user is entitled to access (own or internal/authorized content).

## SharePoint / authenticated enterprise sites

```bash
uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" --list
uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" -o ~/Downloads --title "<part of video title>"
```

- **Not headless**: SharePoint is behind the viewer's own login, so the script opens a real Chrome window and the user signs in once (SSO + MFA). Session persists in `~/Library/Caches/sp-fetch/chrome-profile`, so later runs skip login. Tell the user the window is coming before you run it.
- Validated 2026-10-08 against an enterprise SharePoint Online site (`<tenant>.sharepoint.com/sites/<site>/SitePages/*.aspx`): an unauthenticated request returns **403 + `x-forms_based_auth_required`**, so there is nothing to parse before login — do not try to scrape the page first.
- **Do not sniff network traffic for the video.** These pages only stream after the user clicks play, so a `page.on("response")` listener records zero media. Instead read the rendered HTML: the play button carries `videosrc="..." videotitle="..."` attributes. The script regexes those out. (Also works for the "highlighted content" cards on such pages.)
- Fallback for a whole video library — SharePoint REST, with a cookie jar exported from the logged-in context:
  `…/_api/web/GetFolderByServerRelativeUrl('/sites/<site>/<Library>')/Files?$select=Name,Length,TimeLastModified,ServerRelativeUrl` (`Accept: application/json;odata=nometadata`), then `curl --noproxy '*' -b cookies.txt -L "<ServerRelativeUrl>?download=1"`.
- Gotchas encoded in the script, do not remove:
  - Launch with **`channel="chrome"`** to use the system Google Chrome. The ms-playwright cache often holds a different Chromium build than the pinned playwright version wants (`Executable doesn't exist at …/chromium-12xx/…`); system Chrome avoids the download entirely and is likelier to satisfy corporate conditional-access.
  - `--no-proxy-server` is mandatory (same dead-proxy issue as Douyin).
  - The sandboxed agent environment **blocks reading `~/Library/Application Support/Google/Chrome`** (and `yt-dlp --cookies-from-browser chrome` therefore reports "could not find chrome cookies database"). Do not fight it — use the throwaway profile instead.
  - Delete the exported cookie jar right after the download; keep only the profile dir.
- For an internal file that is simply a link (not embedded), pass the direct `.mp4` URL; the script still logs in first, then downloads.


## Environment gotchas (apply to every attempt)

- A **dead local proxy** (`http_proxy=http://127.0.0.1:*` pointing at no listener) silently hijacks all traffic. If one is configured, every network call must bypass it: curl `--noproxy '*'` (scripts also strip all `*proxy*` env vars), Chromium `--no-proxy-server`, yt-dlp `--proxy ""`.
- On macOS, TCC blocks reading Safari cookie files (Operation not permitted) — don't try `--cookies-from-browser safari`.
- Scripts locate `ffprobe`/`yt-dlp` on PATH first (falling back to `/opt/homebrew/bin/`).
- Scripts are invoked relative to this skill's directory (`scripts/...`); resolve the absolute path to the skill dir when running.

## Douyin

```bash
uv run --with playwright==1.63.0 python scripts/fetch_douyin.py "<share text / v.douyin.com link / video id>" -o <outdir>
```

- Add `--info` to fetch metadata only (title).
- Requires playwright (the `uv run` above provisions it ephemerally; pin 1.63.0 to reuse the cached Chromium in `~/Library/Caches/ms-playwright`) and yt-dlp on PATH. Any Python with playwright also works — in a sandboxed agent environment, use whatever Python venv already has playwright.
- Pipeline: resolve short link → headless Chromium (playwright, `--no-proxy-server`) opens `www.douyin.com/video/{id}` and exports cookies → yt-dlp downloads with those cookies (720p, no watermark, auto-merges).
- Why cookies: Douyin's free endpoints (`iteminfo` API, `aweme/v1/play`, share-page `_ROUTER_DATA`) are all dead as of 2026-10-01; yt-dlp's Douyin extractor works only with fresh cookies.
- Known failures: yt-dlp `HTTP 416` = stale resume state, scripts already pass `--no-continue --no-part`; if playwright import fails, wrong Python is being used — see invocation above.
- If yt-dlp fails with "Fresh cookies needed", the cookie grab failed silently — rerun; if Douyin redesigned the page, check whether `page.goto` lands on a login wall and fall back to asking the user for the app's "复制链接" share text again.

## Xiaohongshu

```bash
python3 scripts/fetch_xhs.py "<share text / xhslink.cn link / note URL>" -o <outdir>
```

- Add `--info` **before** downloading long videos: XHS hosts 1h+ podcasts (e.g. 285 MB); show duration/size first when the note title suggests long-form content.
- `--codec h265` for better compression at the same resolution; default h264 for compatibility.
- Pipeline: resolve short link (redirect carries the required `xsec_token`) → parse `window.__INITIAL_STATE__` (replace JS `undefined` with `null` before `json.loads`) → `noteData.video.media.stream.h264[0].masterUrl` is a pre-signed MP4, download with UA + `Referer: https://www.xiaohongshu.com/`. No login needed.
- Two traps already encoded in the script, do not remove: **(1) mobile UA is mandatory** — a desktop UA gets a stripped page without video data; **(2) the script verifies the served note ID matches the link's note ID** — with a stale token XHS serves a feed page of recommended notes, and without this check you would download the wrong video.
- **Image notes are handled by the same script (validated 2026-10-02)**: when no video stream exists, it falls back to `noteData.data.noteData.imageList` — per image it picks the `WB_DFT` url from `infoList` (fallback `urlDefault`/`url`), downloads all images with UA + `Referer: https://www.xiaohongshu.com/`, and writes a `{title}_note.md` stub (title, source URL, fetch date, full desc). No login needed.
  - Watch out: the note payload lives at `noteData.data.noteData` (double `data`), NOT `noteData.note` — naive parsing finds an empty note.
  - Downloaded images are XHS display-quality (~1000px wide, a few hundred KB), not the 4344px originals — that's all the web page exposes without app-level APIs.
  - `--info` on an image note prints type + image count without downloading.
- Stdlib-only — any Python 3 works.

## WeChat official-account articles (公众号)

```bash
python3 scripts/fetch_wechat.py "<mp.weixin.qq.com/s/... URL or share text>" -o <outdir>
```

- Downloads the **best-quality video** (if the article has one) **and all body images** (if any). `--info` previews title/qualities/image count without downloading; `--video-only` / `--images-only` restrict.
- Stdlib-only (curl via subprocess) — any Python 3 works. No login needed.
- Pipeline (validated 2026-10-08):
  1. Fetch the article HTML with a mobile UA and no proxy.
  2. Video: the page embeds a JS array of quality variants (`{format_id, width, height, filesize, url}`); pick the largest by pixel count. Observed mapping: `10002` = 1920×1080, `10102` = 1080×606, `10004` = 854×480, `10104` = 480×270.
  3. Images: the rendered body lives in the JS field `content_noencode`; decode `\xNN` escapes and take `data-src` URLs on `qpic.cn` (dedupe, keep order). Writes a `wechat_{title}_note.md` stub listing the files.
- Gotchas encoded in the script, do not remove:
  - `mpvideo.qpic.cn` is anti-leech and IP-redirected: the signed URL intermittently returns **403**, or **302 → `http://<edge-ip>:<port>/mpvideo.qpic.cn/...`**. Retry until a 302, then fetch the Location with **`Host: mpvideo.qpic.cn`** — the edge needs the original Host header, not the IP.
  - WeChat article pages intermittently serve a stripped anti-bot page; the script retries the fetch.
  - The signed video URL carries its own expiry; if it fails after retries, ask for a fresh share link.

## Transcription hand-off

- If a **逐字稿** or slide/OCR report is wanted for a downloaded video/audio, hand off to a separate transcription step (e.g. the `video-miner` skill) — do not transcribe here.
- **Image-only** downloads have no transcription downstream. This skill does not OCR.

## Output conventions

- Default output directory: `~/Downloads` (created if missing); pass `-o` to override.
- Verify every download with ffprobe (video) or file size (images) and report in your reply: path, size, duration, resolution.

## Other platforms

Bilibili/YouTube/others: try `yt-dlp --proxy ""` first; only fall back to a browser-based approach like the Douyin script if the extractor fails. Authenticated enterprise sites (SharePoint/Stream/Confluence-style) go to `scripts/fetch_sharepoint.py` above. If a new platform needs custom parsing, extend this skill with a new `scripts/fetch_<platform>.py` following the same conventions (short-link resolution, proxy bypass, ffprobe verification, `~/Downloads` default, stdlib + curl where possible).
