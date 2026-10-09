#!/usr/bin/env python3
"""Download a Xiaohongshu (小红书) note from a share link or note URL.

Video notes -> single MP4. Image notes -> all images + a note.md stub.

Usage:
    fetch_xhs.py <share_url_or_note_url> [-o OUTDIR] [--codec h264|h265] [--info]

Stdlib-only (curl via subprocess) — any Python 3 works.

How it works (validated 2026-10-01 video / 2026-10-02 images):
  1. Resolve xhslink.cn short link -> www.xiaohongshu.com/discovery/item/{id}
     (the redirect carries the xsec_token needed to view the note).
  2. Fetch the note page with a mobile UA and parse window.__INITIAL_STATE__.
     JS `undefined` literals must be replaced with null before json.loads.
  3. Video notes: noteData.video.media.stream.{h264,h265}[0].masterUrl is a
     pre-signed direct MP4 URL (no login needed, but send UA + Referer).
  4. Image notes: noteData.data.noteData.imageList[] — per image pick the
     WB_DFT url from infoList (fallback urlDefault/url), download each with
     UA + Referer, and write a note.md stub with title/desc/source link.
  5. curl download, then verify with ffprobe (video) / file size (images).

Note: XHS hosts long videos too (podcasts/interviews, 1h+). --info shows
duration/resolution before downloading so huge files don't surprise anyone.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
# MOBILE UA IS REQUIRED: desktop UA gets a stripped page without video data.
NO_PROXY_ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, env=NO_PROXY_ENV)


def fetch_note_page(url: str) -> tuple[str, str]:
    """Return (html, final_url). The final URL carries the target note ID."""
    r = sh(["curl", "-s", "--noproxy", "*", "-L", "-A", UA,
            "-o", "/dev/null", "-w", "%{url_effective}", url])
    final_url = r.stdout.strip()
    r = sh(["curl", "-s", "--noproxy", "*", "-L", "-A", UA, final_url])
    html = r.stdout
    if "__INITIAL_STATE__" not in html:
        raise SystemExit("ERROR: note page did not load (no __INITIAL_STATE__). "
                         "The xsec_token may have expired — ask for a fresh share link.")
    return html, final_url


def parse_state(html: str) -> dict:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\(function\(\)", html, re.S) \
        or re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>", html, re.S)
    if not m:
        raise SystemExit("ERROR: cannot locate __INITIAL_STATE__ JSON.")
    raw = re.sub(r"\bundefined\b", "null", m.group(1))
    return json.loads(raw)


def find_video_streams(obj, depth=0):
    """Recursively find the first dict that looks like video.media.stream."""
    if depth > 10:
        return None
    if isinstance(obj, dict):
        if "media" in obj and isinstance(obj.get("media"), dict) \
                and isinstance(obj["media"].get("stream"), dict):
            return obj["media"]["stream"]
        for v in obj.values():
            r = find_video_streams(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_video_streams(v, depth + 1)
            if r:
                return r
    return None


def find_str(obj, key, depth=0):
    if depth > 10:
        return None
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v
        for val in obj.values():
            r = find_str(val, key, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for val in obj:
            r = find_str(val, key, depth + 1)
            if r:
                return r
    return None


def safe_name(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[\\/:*?\"<>|\n\r\t#]", "", s).strip()
    return s[:maxlen] or "xiaohongshu"


def pick_image_url(im: dict) -> str | None:
    """Best URL for one imageList entry: WB_DFT from infoList, then fallbacks."""
    best = None
    for info in im.get("infoList") or []:
        u = info.get("url")
        if u and ("WB_DFT" in u or "nd_dft" in u):
            best = u  # keep the last WB_DFT candidate
    return best or im.get("urlDefault") or im.get("url")


def download_images(target: dict, title: str, final_url: str, outdir: str) -> None:
    """Download all images of an image note + write a note.md stub."""
    images = target.get("imageList") or []
    if not images:
        raise SystemExit("ERROR: no video stream and no images found.")
    os.makedirs(outdir, exist_ok=True)
    base = f"xhs_{safe_name(title)}"
    saved = []
    for i, im in enumerate(images):
        url = pick_image_url(im)
        if not url:
            print(f"[{i}] WARNING: no usable URL, skipped")
            continue
        ext = ".webp" if ".webp" in url else ".jpg"
        out = os.path.join(outdir, f"{base}_{i:02d}{ext}")
        r = sh(["curl", "-s", "--noproxy", "*", "-A", UA,
                "-e", "https://www.xiaohongshu.com/", "-o", out, url])
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
            print(f"[{i}] WARNING: download failed: {url}")
            continue
        saved.append(out)
        print(f"[{i}/{len(images)}] {out} ({os.path.getsize(out) / 1024:.0f} KB, "
              f"{im.get('width')}x{im.get('height')})")
    if not saved:
        raise SystemExit("ERROR: all image downloads failed.")
    note_md = os.path.join(outdir, f"{base}_note.md")
    desc = (find_str(target, "desc") or "").strip()
    with open(note_md, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"- 来源: {final_url}\n")
        f.write(f"- 抓取日期: {datetime.date.today().isoformat()}\n")
        f.write(f"- 图片数: {len(saved)}\n\n")
        f.write((desc or "(无正文)") + "\n")
    print(f"OK: {len(saved)}/{len(images)} images -> {outdir}")
    print(f"OK: {note_md}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="share text/short link/note URL")
    ap.add_argument("-o", "--outdir", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("--codec", choices=["h264", "h265"], default="h264",
                    help="h264 = max compatibility (default), h265 = same res, better compression")
    ap.add_argument("--info", action="store_true", help="show metadata only, no download")
    args = ap.parse_args()

    html, final_url = fetch_note_page(args.url.strip())
    state = parse_state(html)

    # Verify the page actually contains the TARGET note (a stale/expired token
    # can make XHS serve a feed page with recommended notes instead — never
    # download those by mistake).
    m = re.search(r"/discovery/item/([0-9a-f]{24})", final_url)
    target = state.get("noteData", {}).get("data", {}).get("noteData", {}) or {}
    if m and target.get("noteId") != m.group(1):
        raise SystemExit(
            f"ERROR: page served note {target.get('noteId')} but link points to "
            f"{m.group(1)}. The share token likely expired — ask for a fresh share link.")

    title = (find_str(target, "title") if target else None) \
        or safe_name(find_str(target, "desc") or find_str(state, "title") or "xiaohongshu")
    title = (title or "").strip() or "xiaohongshu"

    stream = find_video_streams(target or state)
    if not stream:
        n_imgs = len(target.get("imageList") or [])
        print(f"title: {title[:80]}")
        print(f"type: image note, {n_imgs} images")
        if args.info:
            return
        download_images(target, title, final_url, args.outdir)
        return
    entries = stream.get(args.codec) or stream.get("h264") or stream.get("h265")
    if not entries:
        raise SystemExit(f"ERROR: no {args.codec} stream entries.")
    meta = entries[0]
    url = meta.get("masterUrl")
    if not url:
        raise SystemExit("ERROR: stream entry has no masterUrl.")

    w, h = meta.get("width"), meta.get("height")
    try:
        dur = float(meta.get("duration") or 0)
        if dur > 10000:  # milliseconds
            dur /= 1000
    except (TypeError, ValueError):
        dur = 0.0
    print(f"title: {title[:80]}")
    print(f"codec: {args.codec}, resolution: {w}x{h}, duration: {dur:.0f}s")
    if args.info:
        return

    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, f"xhs_{safe_name(title)}_{h}p.mp4")
    r = sh(["curl", "-s", "--noproxy", "*", "-A", UA,
            "-e", "https://www.xiaohongshu.com/", "-o", out, url])
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 10000:
        raise SystemExit(f"ERROR: download failed: {out}")

    v = sh([FFPROBE, "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", out])
    size = os.path.getsize(out) / 1e6
    print(f"OK: {out} ({size:.1f} MB, {float(v.stdout or 0):.0f}s)")


if __name__ == "__main__":
    main()
