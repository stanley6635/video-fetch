#!/usr/bin/env python3
"""Download a Douyin (抖音) video from a share link or video ID.

Usage:
    fetch_douyin.py <share_url_or_video_id> [-o OUTDIR] [--info]

Must be run with a Python that has playwright installed, e.g.:
    uv run --with playwright==1.63.0 python fetch_douyin.py <url>
(any Python with playwright also works; 1.63.0 matches the cached Chromium
in ~/Library/Caches/ms-playwright so no browser re-download is needed)

How it works (validated 2026-10-01):
  1. Resolve v.douyin.com short link -> real video ID.
  2. Open https://www.douyin.com/video/{id} in headless Chromium (playwright)
     and export session cookies. Douyin's free endpoints (iteminfo API,
     aweme/v1/play) are dead; yt-dlp needs fresh cookies to parse the page.
  3. Hand cookies to yt-dlp to download & merge (up to 720p, no watermark).

Gotchas encoded here (do not remove):
  - All network calls bypass the environment proxy (local dead proxy hijacks
    everything): curl uses --noproxy '*', Chromium gets --no-proxy-server,
    yt-dlp gets --proxy "".
  - yt-dlp may hit HTTP 416 from a stale resume state: always use
    --no-continue --no-part.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
NO_PROXY_ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, env=NO_PROXY_ENV, **kw)


def resolve_video_id(arg: str) -> str:
    if re.fullmatch(r"\d{15,25}", arg):
        return arg
    m = re.search(r"/video/(\d+)", arg)
    if m:
        return m.group(1)
    # short link: follow redirect (HEAD may be blocked; fall back to GET)
    r = sh(["curl", "-sI", "--noproxy", "*", "-o", "/dev/null",
            "-w", "%{redirect_url}", arg])
    url = r.stdout.strip()
    m = re.search(r"/(?:video|note|slides)/(\d+)", url)
    if not m:
        r2 = sh(["curl", "-s", "--noproxy", "*", "-L", "-o", "/dev/null",
                 "-w", "%{url_effective}", "-A", UA, arg])
        m = re.search(r"/(?:video|note|slides)/(\d+)", r2.stdout)
        url = r2.stdout
    if not m:
        raise SystemExit(f"ERROR: cannot resolve video ID from: {arg}\nlast url: {url}")
    return m.group(1)


def find_chromium() -> str:
    pats = sorted(
        glob.glob(os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/*.app/Contents/MacOS/*")),
        reverse=True)
    exe = [p for p in pats if os.access(p, os.X_OK) and "Helper" not in p]
    if not exe:
        raise SystemExit("ERROR: no playwright Chromium found. Run `playwright install chromium`.")
    return exe[0]


def grab_cookies(video_id: str) -> tuple[str, str]:
    """Open the video page headlessly, return (cookies_file, page_title)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "ERROR: playwright not available. Run this script with:\n"
            "  uv run --with playwright==1.63.0 python fetch_douyin.py <url>\n"
            "(or any Python that has playwright installed)")
    url = f"https://www.douyin.com/video/{video_id}"
    cookies_file = tempfile.gettempdir() + f"/douyin_cookies_{video_id}.txt"
    title = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=find_chromium(),
                                    headless=True, args=["--no-proxy-server"])
        ctx = browser.new_context(user_agent=UA,
                                  viewport={"width": 1440, "height": 900},
                                  locale="zh-CN")
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(9000)
        try:
            title = page.title()
        except Exception:
            pass
        cookies = ctx.cookies("https://www.douyin.com")
        browser.close()
    with open(cookies_file, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            flag = "TRUE" if c.get("secure") else "FALSE"
            sub = "TRUE" if c["domain"].startswith(".") else "FALSE"
            exp = int(c["expires"]) if c.get("expires", -1) > 0 else 2147483647
            f.write(f"{c['domain']}\t{sub}\t{c.get('path','/')}\t{flag}\t{exp}\t{c['name']}\t{c['value']}\n")
    return cookies_file, title


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="share text/short link/video page URL/video ID")
    ap.add_argument("-o", "--outdir", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("--info", action="store_true", help="print title only, no download")
    args = ap.parse_args()

    vid = resolve_video_id(args.url.strip())
    print(f"video id: {vid}")
    cookies_file, title = grab_cookies(vid)
    if title:
        print("title:", title[:100])
    if args.info:
        return

    os.makedirs(args.outdir, exist_ok=True)
    tpl = os.path.join(args.outdir, "douyin_%(id)s.%(ext)s")
    for attempt in range(2):
        r = sh([YTDLP, "--proxy", "",
                "--cookies", cookies_file, "--no-playlist",
                "--no-continue", "--no-part", "--no-mtime",
                "-o", tpl, f"https://www.douyin.com/video/{vid}"])
        if r.returncode == 0 and re.search(r"100%", r.stdout):
            break
        print(r.stdout[-500:], r.stderr[-300:])
    else:
        raise SystemExit("ERROR: yt-dlp failed after retry")

    out = os.path.join(args.outdir, f"douyin_{vid}.mp4")
    v = sh([FFPROBE, "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", out])
    size = os.path.getsize(out) / 1e6
    print(f"OK: {out} ({size:.1f} MB, {float(v.stdout or 0):.0f}s)")


if __name__ == "__main__":
    main()
