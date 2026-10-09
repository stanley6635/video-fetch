#!/usr/bin/env python3
"""Download media from a WeChat official-account (公众号) article.

Article -> best-quality video (if present) + all body images (if present).

Usage:
    fetch_wechat.py "<mp.weixin.qq.com/s/... URL or share text>" [-o OUTDIR]
        [--info] [--video-only] [--images-only]

Stdlib-only (curl via subprocess) -- any Python 3 works. This skill only
downloads; it does not transcribe or OCR.

How it works (validated 2026-10-08):
  1. Fetch the article HTML with a mobile UA and no proxy.
  2. Video: the page embeds a JS array of quality variants
     ({format_id, width, height, filesize, url}); pick the largest by pixel
     count. mpvideo.qpic.cn is anti-leech and IP-redirected: requesting the
     signed URL with UA + Referer intermittently answers 403, or 302 to
     http://<edge-ip>:<port>/mpvideo.qpic.cn/...  Retry until a 302, then
     fetch the Location with `Host: mpvideo.qpic.cn` (the edge needs the
     original Host header, not the IP). Observed qualities for one article:
     format_id 10002 = 1920x1080, 10102 = 1080x606, 10004 = 854x480,
     10104 = 480x270.
  3. Images: the rendered body lives in the JS field `content_noencode`;
     decode \\xNN escapes and take `data-src` URLs on qpic.cn (dedupe, keep
     order). Download each with UA + Referer; no login needed.

Gotchas encoded here (do not remove):
  - All network calls bypass the environment proxy (a local dead proxy
    hijacks everything): env proxy vars stripped + curl `--noproxy '*'`.
  - The signed video URL 403s intermittently; the retry loop is required.
  - The CDN edge 302 is only usable with `Host: mpvideo.qpic.cn`.
"""

import argparse
import datetime
import html as htmllib
import os
import re
import shutil
import subprocess
import time
import urllib.parse

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
REF = "https://mp.weixin.qq.com/"
NO_PROXY_ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>]*")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, env=NO_PROXY_ENV)


def extract_url(arg: str) -> str:
    m = URL_RE.search(arg)
    if not m:
        raise SystemExit(f"ERROR: no mp.weixin.qq.com article URL found in: {arg!r}")
    return m.group(0)


def fetch_html(url: str, attempts: int = 5) -> str:
    # WeChat intermittently serves a stripped anti-bot page; retry before failing.
    for _ in range(attempts):
        r = sh(["curl", "-s", "--noproxy", "*", "-L", "-A", UA, url])
        html = r.stdout
        if "og:title" in html or "content_noencode" in html:
            return html
        time.sleep(2)
    raise SystemExit("ERROR: article page did not load (no article markers). "
                     "The link may be expired or require a fresh share link.")


def get_title(html: str) -> str:
    for pat in (r'<meta property="og:title" content="([^"]*)"',
                r"var\s+msg_title\s*=\s*['\"]([^'\"]*)['\"]"):
        m = re.search(pat, html)
        if m and m.group(1).strip():
            return htmllib.unescape(m.group(1)).strip()
    m = re.search(r"<title>([^<]*)</title>", html)
    return htmllib.unescape(m.group(1)).strip() if m else "wechat"


def _unescape_js_url(u: str) -> str:
    u = u.replace("\\x26amp;", "&").replace("\\x26", "&").replace("&amp;", "&")
    return u.replace("\\/", "/")


def parse_videos(html: str) -> list[dict]:
    """Quality variants from the embedded JS array. Largest pixels first."""
    out = []
    for block in re.split(r"format_id:", html)[1:]:
        u = re.search(r"url:\s*'(http://mpvideo\.qpic\.cn/[^']+\.mp4[^']*)'", block)
        if not u:
            continue
        fid = re.match(r"\s*'(\d+)'", block)
        w = re.search(r"width:\s*'(\d+)'", block)
        h = re.search(r"height:\s*'(\d+)'", block)
        fs = re.search(r"filesize:\s*'(\d+)'", block)
        out.append({
            "format_id": fid.group(1) if fid else "?",
            "width": int(w.group(1)) if w else 0,
            "height": int(h.group(1)) if h else 0,
            "filesize": int(fs.group(1)) if fs else 0,
            "url": _unescape_js_url(u.group(1)),
        })
    seen, uniq = set(), []
    for v in out:
        if v["url"] in seen:
            continue
        seen.add(v["url"])
        uniq.append(v)
    uniq.sort(key=lambda v: (v["width"] * v["height"], v["filesize"]), reverse=True)
    return uniq


def parse_images(html: str) -> list[str]:
    """Body image `data-src` URLs on qpic.cn, deduped, in order."""
    m = re.search(r"content_noencode:\s*'((?:[^'\\]|\\.)*)'", html, re.S)
    body = m.group(1) if m else html
    body = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), body)
    urls = [u.replace("&amp;", "&") for u in re.findall(r'data-src="([^"]+)"', body)]
    urls = [u for u in urls if "qpic.cn" in u]
    seen, uniq = set(), []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


def safe_name(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[\\/:*?\"<>|\n\r\t#]", "", s).strip()
    return s[:maxlen] or "wechat"


def img_ext(u: str) -> str:
    m = re.search(r"wx_fmt=([a-zA-Z0-9]+)", u)
    if m:
        ext = m.group(1).lower()
        return ".jpg" if ext in ("jpeg", "jpg") else f".{ext}"
    path = urllib.parse.urlparse(u).path
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp") else ".jpg"


def download_video(entry: dict, outdir: str, title: str) -> str | None:
    h = entry["height"] or 0
    out = os.path.join(outdir, f"wechat_{safe_name(title)}_{h}p.mp4")
    for _ in range(12):
        r = sh(["curl", "-s", "--noproxy", "*", "-o", "/dev/null",
                "-w", "%{http_code}\t%{redirect_url}",
                "-A", UA, "-e", REF, entry["url"]])
        code, _, loc = r.stdout.strip().partition("\t")
        if code == "302" and loc:
            sh(["curl", "-s", "--noproxy", "*", "-L", "-o", out,
                "-A", UA, "-e", REF, "-H", "Host: mpvideo.qpic.cn", loc])
        elif code == "200":
            sh(["curl", "-s", "--noproxy", "*", "-o", out, "-A", UA, "-e", REF, entry["url"]])
        if os.path.exists(out) and os.path.getsize(out) > 10000:
            return out
        time.sleep(1.5)
    return None


def download_images(urls: list[str], outdir: str, title: str) -> list[str]:
    base = f"wechat_{safe_name(title)}"
    saved = []
    for i, u in enumerate(urls):
        out = os.path.join(outdir, f"{base}_img_{i:02d}{img_ext(u)}")
        r = sh(["curl", "-s", "--noproxy", "*", "-A", UA, "-e", REF, "-o", out, u])
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
            print(f"[{i}] WARNING: download failed: {u}")
            continue
        saved.append(out)
        print(f"[{i}/{len(urls)}] {out} ({os.path.getsize(out) / 1024:.0f} KB)")
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="mp.weixin.qq.com article URL or share text")
    ap.add_argument("-o", "--outdir", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("--info", action="store_true", help="show metadata only, no download")
    ap.add_argument("--video-only", action="store_true", help="skip image download")
    ap.add_argument("--images-only", action="store_true", help="skip video download")
    args = ap.parse_args()

    url = extract_url(args.url.strip())
    html = fetch_html(url)
    title = get_title(html)
    videos = parse_videos(html)
    images = parse_images(html)

    print(f"title: {title[:100]}")
    print(f"video: {len(videos)} quality variant(s)" +
          (f", best {videos[0]['width']}x{videos[0]['height']} "
           f"({videos[0]['filesize'] / 1e6:.1f} MB)" if videos else ""))
    print(f"images: {len(images)}")
    if args.info:
        for v in videos:
            print(f"  video f{v['format_id']}: {v['width']}x{v['height']} "
                  f"{v['filesize'] / 1e6:.1f} MB")
        return

    if not videos and not images:
        raise SystemExit("ERROR: article contains no downloadable video or image.")

    os.makedirs(args.outdir, exist_ok=True)
    results = []

    if videos and not args.images_only:
        out = download_video(videos[0], args.outdir, title)
        if not out:
            raise SystemExit("ERROR: video download failed after retries "
                             "(signed URL may have expired).")
        v = sh([FFPROBE, "-v", "error", "-show_entries",
                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", out])
        print(f"OK video: {out} ({os.path.getsize(out) / 1e6:.1f} MB, "
              f"{float(v.stdout or 0):.0f}s, {videos[0]['width']}x{videos[0]['height']})")
        results.append(os.path.basename(out))

    if images and not args.video_only:
        saved = download_images(images, args.outdir, title)
        if not saved:
            raise SystemExit("ERROR: all image downloads failed.")
        results += [os.path.basename(p) for p in saved]
        print(f"OK images: {len(saved)}/{len(images)} -> {args.outdir}")

    if images and not args.video_only:
        stub = os.path.join(args.outdir, f"wechat_{safe_name(title)}_note.md")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n")
            f.write(f"- 来源: {url}\n")
            f.write(f"- 抓取日期: {datetime.date.today().isoformat()}\n")
            f.write(f"- 文件: {len(results)}\n\n")
            for name in results:
                f.write(f"- {name}\n")
        print(f"OK: {stub}")


if __name__ == "__main__":
    main()
