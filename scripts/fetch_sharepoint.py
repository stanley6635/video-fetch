#!/usr/bin/env python3
"""Download a video embedded in a SharePoint Online page (e.g. an internal comms site).

SharePoint sites are behind the viewer's own login, so unlike the Douyin/XHS/WeChat
scripts this one cannot be fully headless: it opens a **real Chrome window** (system
Google Chrome, with its own throwaway profile) and waits for the user to sign in once
(SSO + MFA). The session is kept in the profile dir, so later runs skip the login.

Video discovery: SharePoint custom pages usually embed videos as a plain attribute on
the play button, e.g.
    <img class="play-btn" videosrc="https://<tenant>.sharepoint.com/sites/x/Lib/foo.mp4"
         videotitle="Quarterly All Hands">
so we read the rendered page HTML instead of sniffing network traffic (nothing streams
until the user clicks play, so a response listener sees no media at all).

Usage:
  uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" --list
  uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" -o ~/Downloads
  uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<page URL>" --title "Quarterly All Hands"
  uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<direct .mp4 URL>"

Options:
  -o DIR        output directory (default ~/Downloads)
  --profile D   Chrome profile dir for the session (default ~/Library/Caches/sp-fetch/chrome-profile)
  --list        only list the videos found on the page, download nothing
  --index N     pick the Nth video found (0-based, page order)
  --title TEXT  pick the video whose videotitle contains TEXT (case-insensitive)
  --timeout S   seconds to wait for the user to finish signing in (default 900)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse

DEFAULT_PROFILE = os.path.expanduser("~/Library/Caches/sp-fetch/chrome-profile")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

# videosrc="..." and the nearest following videotitle="..."
VIDEO_RE = re.compile(r'videosrc="([^"]+)"', re.I)
TITLE_RE = re.compile(r'videotitle="([^"]*)"', re.I)


def log(*a):
    print(*a, flush=True)


def need_playwright():
    try:
        import playwright  # noqa: F401
    except ImportError:
        log("playwright not importable. Run this script as:")
        log('  uv run --with playwright==1.63.0 python scripts/fetch_sharepoint.py "<url>"')
        sys.exit(2)


def wait_for_login(page, ctx, domain, timeout):
    """Poll until the page URL sits on the target tenant and is not a login page."""
    deadline = time.time() + timeout
    log(">>> A Chrome window is open. Sign in to SharePoint there (SSO + MFA).")
    while time.time() < deadline:
        time.sleep(3)
        if not ctx.pages:
            log("browser window was closed")
            return False
        try:
            url = page.url
        except Exception:
            try:
                page = ctx.pages[0]
                url = page.url
            except Exception:
                return False
        low = url.lower()
        if domain in low and "login" not in low and "_forms" not in low and "/sas/" not in low:
            log("signed in:", url[:110])
            return True
        log("  waiting…", url[:110])
    log("timed out waiting for login")
    return False


def find_videos(html):
    out = []
    for m in VIDEO_RE.finditer(html):
        url = m.group(1)
        tm = TITLE_RE.search(html, m.end(), m.end() + 400)
        title = tm.group(1) if tm else ""
        if not any(v["url"] == url for v in out):
            out.append({"url": url, "title": title})
    return out


def export_cookies(ctx):
    fd, path = tempfile.mkstemp(prefix="sp_cookies_", suffix=".txt")
    os.close(fd)
    with open(path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in ctx.cookies():
            dom = c.get("domain", "")
            flag = "TRUE" if dom.startswith(".") else "FALSE"
            sec = "TRUE" if c.get("secure") else "FALSE"
            exp = int(c.get("expires") or 0) or int(time.time()) + 86400
            f.write("\t".join([dom, flag, c.get("path", "/"), sec, str(exp),
                               c.get("name", ""), c.get("value", "")]) + "\n")
    return path


def curl_download(url, jar, outpath):
    cmd = ["curl", "--noproxy", "*", "-L", "-sS", "--fail",
           "--max-time", "3600", "-b", jar, "-H", "User-Agent: " + UA,
           "-o", outpath, "-w", "%{http_code} %{size_download} %{content_type}",
           url + ("&" if "?" in url else "?") + "download=1"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def ffprobe(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,size:stream=codec_type,codec_name,width,height",
             "-of", "json", path],
            capture_output=True, text=True)
        return json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


def safe_name(name):
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip(" .")
    return name or "sharepoint_video"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="SharePoint page URL (or a direct .mp4 URL)")
    ap.add_argument("-o", "--outdir", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--index", type=int, default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    need_playwright()
    from playwright.sync_api import sync_playwright

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.profile, exist_ok=True)
    host = urlparse(args.url).netloc.lower()
    direct = urlparse(args.url).path.lower().endswith((".mp4", ".m4v", ".mov", ".webm"))

    with sync_playwright() as p:
        common = dict(headless=False,
                      args=["--no-proxy-server", "--disable-blink-features=AutomationControlled"],
                      viewport={"width": 1400, "height": 950})
        try:
            ctx = p.chromium.launch_persistent_context(args.profile, channel="chrome", **common)
        except Exception as e:
            log("system Chrome unavailable (%s); using bundled chromium" % e)
            ctx = p.chromium.launch_persistent_context(args.profile, **common)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            log("goto warning:", e)
        if not wait_for_login(page, ctx, host, args.timeout):
            ctx.close()
            sys.exit(3)

        job = None
        if direct:
            job = {"url": args.url, "title": unquote(os.path.basename(urlparse(args.url).path))}
        else:
            time.sleep(8)  # let web parts render
            html = page.content()
            videos = find_videos(html)
            if not videos:
                log("no videosrc=\"...\" attribute found on the page.")
                log("Dumping page HTML to /tmp for manual inspection.")
                open("/tmp/sharepoint_page.html", "w").write(html)
                ctx.close()
                sys.exit(4)
            log("videos found on page:")
            for i, v in enumerate(videos):
                log("  [%d] %s  <-  %s" % (i, v["title"] or "(no title)", v["url"].rsplit("/", 1)[-1]))
            if args.list:
                ctx.close()
                return
            if args.title:
                hits = [v for v in videos if args.title.lower() in v["title"].lower()]
                if not hits:
                    log("no video title contains %r" % args.title)
                    ctx.close()
                    sys.exit(5)
                job = hits[0]
            elif args.index is not None:
                job = videos[args.index]
            else:
                job = videos[0]

        jar = export_cookies(ctx)
        ctx.close()

    name = safe_name(job["title"] or unquote(os.path.basename(urlparse(job["url"]).path)))
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    outpath = os.path.join(args.outdir, name)

    try:
        log("downloading:", job["url"].rsplit("/", 1)[-1])
        rc, out, err = curl_download(job["url"], jar, outpath)
        if rc != 0:
            log("curl failed rc=%s %s %s" % (rc, out, err))
            sys.exit(6)
        log("curl:", out)
    finally:
        os.unlink(jar)

    info = ffprobe(outpath)
    size = os.path.getsize(outpath)
    log("\n==== done ====")
    log("path  :", outpath)
    log("size  : %.1f MB (%d bytes)" % (size / 1048576, size))
    if info:
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                log("video : %s %sx%s" % (s.get("codec_name"), s.get("width"), s.get("height")))
            elif s.get("codec_type") == "audio":
                log("audio : %s" % s.get("codec_name"))
        d = float(info.get("format", {}).get("duration") or 0)
        log("length: %d:%02d" % (d // 60, d % 60))


if __name__ == "__main__":
    main()
