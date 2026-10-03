"""依播放清單下載繁中字幕，標示會員影片，另存 UTF-8 純文字。

只需要 Python 標準函式庫，以及 PATH 中的 yt-dlp、Node.js。
原始 VTT 留在各分類資料夾；.cache 保留字幕快取，重跑時可重用。
"""

from __future__ import annotations

import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse


LANGUAGES = ("zh-Hant", "zh-TW", "zh-HK")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
TIMESTAMP = r"(?:\d+:)?\d{2}:\d{2}[.,]\d{3}"
TIMING = re.compile(rf"^\s*({TIMESTAMP})\s+-->\s+({TIMESTAMP})(?:\s|$)")
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)


def safe_name(value: str, limit: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value[:limit].strip().rstrip(". ") or "未命名"
    if RESERVED.fullmatch(value.split(".")[0]):
        value = "_" + value
    return value


def vtt_to_text(source: str, *, automatic: bool = False) -> str:
    """移除 VTT 時間碼/標籤；自動字幕合併相鄰的滾動重複行。"""
    source = source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    previous: list[str] = []
    previous_end: float | None = None
    for block in re.split(r"\n[ \t]*\n", source):
        lines = block.splitlines()
        if not lines or re.match(r"^(WEBVTT|NOTE|STYLE|REGION)(?:\s|$)", lines[0]):
            continue
        timing_index = next((i for i, line in enumerate(lines) if TIMING.match(line)), None)
        if timing_index is None:
            continue
        timing = TIMING.match(lines[timing_index])
        assert timing is not None
        times = []
        for stamp in timing.groups():
            seconds = 0.0
            for part in stamp.replace(",", ".").split(":"):
                seconds = seconds * 60 + float(part)
            times.append(seconds)
        contiguous = previous_end is not None and times[0] <= previous_end + 0.02
        if not contiguous:
            previous = []
        cue = []
        for line in lines[timing_index + 1:]:
            text = html.unescape(re.sub(r"<[^>]*>", "", line))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                cue.append(text)
        overlap = 0
        if automatic:
            for size in range(min(len(previous), len(cue)), 0, -1):
                if previous[-size:] == cue[:size]:
                    overlap = size
                    break
        for text in cue[overlap:]:
            if automatic and output and previous and output[-1] == previous[-1]:
                if text == output[-1] or output[-1].startswith(text):
                    continue
                if text.startswith(output[-1]):
                    output[-1] = text
                    continue
            output.append(text)
        previous = cue
        previous_end = times[1]
    return "\n".join(output) + ("\n" if output else "")


def run_json(command: list[str], handler) -> int:
    """JSON 留在記憶體；yt-dlp 的警告/錯誤直接顯示於終端。"""
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", env=environment,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                if line.strip():
                    print(line.rstrip(), flush=True)
                continue
            if isinstance(data, dict):
                handler(data)
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout is not None:
            process.stdout.close()


def fetch_lists(common: list[str], urls: list[str]) -> tuple[list[dict], int]:
    roots: list[dict] = []
    if not urls:
        return roots, 0
    status = run_json(
        common + ["--flat-playlist", "--dump-single-json", "--skip-download", *urls],
        roots.append,
    )
    return roots, status


def add_entries(catalog: dict[str, dict], root: dict, channel_id: str,
                group: str | None = None, *, channel_feed: bool = False) -> None:
    for entry in root.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id") or ""
        if not VIDEO_ID.fullmatch(video_id):
            continue
        # 公開播放清單可能引用別的頻道；維持原本下載此頻道的範圍。
        owner = entry.get("channel_id")
        if not channel_feed and channel_id and owner and owner != channel_id:
            continue
        item = catalog.setdefault(video_id, {"groups": set(), "member": False})
        if group:
            item["groups"].add(group)
        item["member"] |= entry.get("availability") == "subscriber_only"


def playlist_id(entry: dict) -> str:
    value = entry.get("id") or ""
    if not value:
        value = parse_qs(urlparse(entry.get("url") or "").query).get("list", [""])[0]
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else ""


def discover(common: list[str], channel: str) -> tuple[dict[str, dict], bool]:
    print("正在取得頻道影片與播放清單…", flush=True)
    roots, status = fetch_lists(common, [channel + "/playlists", channel + "/videos"])
    catalog: dict[str, dict] = {}
    listings: dict[str, dict] = {}
    channel_id = ""
    have_videos = have_playlists = False
    for root in roots:
        channel_id = channel_id or root.get("channel_id") or ""
        source = root.get("original_url") or root.get("webpage_url") or ""
        if urlparse(source).path.rstrip("/").endswith("/playlists"):
            have_playlists = True
            for entry in root.get("entries") or []:
                if isinstance(entry, dict) and (key := playlist_id(entry)):
                    listings[key] = entry
        else:
            have_videos = True
            add_entries(catalog, root, channel_id, channel_feed=True)
    if not roots:
        raise RuntimeError("無法讀取頻道；請先確認網址與 Opera 登入狀態。")
    incomplete = bool(status) or not have_videos or not have_playlists
    if incomplete:
        print("提醒：部分頻道清單未能讀取，本次分類可能不完整。", flush=True)

    names = {key: safe_name(entry.get("title") or key) for key, entry in listings.items()}
    counts = Counter(name.casefold() for name in names.values())
    groups = {
        key: name if counts[name.casefold()] == 1 and name.casefold() not in ("未分類", ".cache")
        else f"{name[:50]} [{key}]"
        for key, name in names.items()
    }
    urls = ["https://www.youtube.com/playlist?list=" + key for key in listings]
    if channel_id.startswith("UC"):
        # YouTube 提供的頻道會員影片清單；有權限時可列出會員影片。
        urls.append("https://www.youtube.com/playlist?list=UUMO" + channel_id[2:])
    print(f"正在讀取 {len(listings)} 個播放清單與會員影片清單…", flush=True)
    details, detail_status = fetch_lists(common, urls)
    received = set()
    for root in details:
        key = root.get("id") or ""
        if key in groups:
            received.add(key)
            add_entries(catalog, root, channel_id, groups[key])
        else:
            add_entries(catalog, root, channel_id)
    if received != set(groups):
        incomplete = True
        print("提醒：有播放清單無法讀取；已取得的影片仍會繼續處理。", flush=True)
    elif detail_status:
        print("提醒：會員影片清單可能無法存取；已取得的會員標記仍會保留。", flush=True)
    return catalog, incomplete


def save_subtitles(info: dict, item: dict, output: Path, cache: Path) -> int:
    video_id = info.get("id") or ""
    if not VIDEO_ID.fullmatch(video_id):
        return 0
    member = item["member"] or info.get("availability") == "subscriber_only"
    prefix = "[會員] " if member else ""
    title = safe_name(info.get("title") or video_id, 100)
    stem = f"{prefix}{title} [{video_id}]"
    folders = sorted(item["groups"]) or ["未分類"]
    written = 0
    for language, subtitle in (info.get("requested_subtitles") or {}).items():
        if language not in LANGUAGES or not isinstance(subtitle, dict):
            continue
        source = cache / f"{video_id}.{language}.vtt"
        if not source.is_file():
            continue
        automatic = language not in (info.get("subtitles") or {})
        text = vtt_to_text(source.read_text(encoding="utf-8-sig"), automatic=automatic)
        if not text.strip():
            print(f"提醒：字幕沒有可轉換的內容：{stem} ({language})", flush=True)
            continue
        for folder_name in folders:
            folder = output / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{stem}.{language}.vtt"
            shutil.copy2(source, target)
            target.with_suffix(".txt").write_text(text, encoding="utf-8-sig")
            written += 1
    return written


def download(common: list[str], catalog: dict[str, dict], output: Path) -> bool:
    cache = output / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    saved = missing = failed = 0

    def on_video(info: dict) -> None:
        nonlocal saved, missing, failed
        video_id = info.get("id") or ""
        if video_id not in catalog:
            return
        seen.add(video_id)
        try:
            count = save_subtitles(info, catalog[video_id], output, cache)
        except (OSError, UnicodeError) as error:
            failed += 1
            print(f"無法儲存字幕 [{video_id}]：{error}", flush=True)
            return
        if count:
            saved += 1
            label = "已儲存 VTT / TXT"
        elif info.get("requested_subtitles"):
            failed += 1
            label = "字幕檔未完整下載或內容無法轉換"
        else:
            missing += 1
            label = "沒有可用的繁中字幕"
        print(f"[{len(seen)}/{len(catalog)}] {label}：{info.get('title') or video_id}", flush=True)

    batch_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".urls",
                                         dir=cache, delete=False) as batch:
            batch_path = Path(batch.name)
            for video_id in catalog:
                batch.write(f"https://www.youtube.com/watch?v={video_id}\n")
        status = run_json(common + [
            "--batch-file", str(batch_path), "--no-playlist",
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", ",".join(LANGUAGES), "--sub-format", "vtt",
            "--no-overwrites", "--no-simulate",
            "--output", str(cache / "%(id)s.%(ext)s"),
            "--print", "after_video:%()j",
        ], on_video)
    finally:
        if batch_path is not None:
            batch_path.unlink(missing_ok=True)
    failed += len(set(catalog) - seen)
    print(f"\n完成：{saved} 支影片已輸出字幕與 TXT；{missing} 支無繁中字幕；"
          f"{failed} 支未成功處理。\n儲存位置：{output}", flush=True)
    return bool(status or failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="https://www.youtube.com/@lqmama550")
    parser.add_argument("--browser", default="opera", help="已登入會員帳號的瀏覽器")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "字幕")
    parser.add_argument("--limit", type=int, help="只處理前 N 支影片，供小量試跑")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必須大於 0")
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise RuntimeError("找不到 yt-dlp，請確認已安裝並加入 PATH。")
    parsed = urlparse(args.channel)
    if parsed.hostname not in ("youtube.com", "www.youtube.com"):
        parser.error("--channel 請使用 YouTube 頻道網址")
    path = re.sub(r"/(videos|playlists|membership|streams|shorts)/?$", "", parsed.path)
    if not re.fullmatch(r"/(?:@[^/]+|(?:channel|c|user)/[^/]+)", path):
        parser.error("--channel 請填頻道網址，例如 https://www.youtube.com/@lqmama550")
    channel = "https://www.youtube.com" + path.rstrip("/")
    common = [
        executable, "--ignore-config", "--encoding", "utf-8", "--no-colors",
        "--js-runtimes", "node", "--cookies-from-browser", args.browser,
        "--ignore-errors", "--socket-timeout", "20", "--retries", "3",
    ]
    catalog, incomplete = discover(common, channel)
    if not catalog:
        raise RuntimeError("沒有找到可處理的影片；請確認頻道網址與觀看權限。")
    if args.limit is not None:
        catalog = dict(list(catalog.items())[:args.limit])
    print(f"\n共 {len(catalog)} 支影片；開始下載繁中字幕並轉換 TXT。", flush=True)
    return int(download(common, catalog, args.output.resolve()) or incomplete)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中止；下次執行會重用已下載的字幕。", file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError) as error:
        print(f"\n錯誤：{error}", file=sys.stderr)
        sys.exit(1)
