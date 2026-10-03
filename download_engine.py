"""GUI 使用的 yt-dlp 執行器；所有外部程式都以參數陣列執行。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse

from _download_subtitles import safe_name, vtt_to_text


LANGUAGE_CHOICES = {
    "繁體中文": "zh-Hant.*,zh-TW.*,zh-HK.*",
    "所有中文字幕（繁／簡）": "zh.*,cmn.*,yue.*",
    "全部語言": "all,-live_chat",
    "英文": "en.*",
}
BROWSERS = {"Opera": "opera", "Chrome": "chrome", "Edge": "edge", "Firefox": "firefox",
            "Brave": "brave", "Vivaldi": "vivaldi", "不使用登入": "", "cookies.txt": "file"}
VIDEO_BITRATE_CHOICES = {
    "最高品質（保留來源）": 0,
    "最省空間（480p／500 kbps）": -1,
    "500 kbps": 500, "1 Mbps": 1000, "2 Mbps": 2000,
    "4 Mbps": 4000, "8 Mbps": 8000, "16 Mbps": 16000,
}
AUDIO_BITRATE_CHOICES = {
    "最高品質": 0, "最省空間（64 kbps）": 64,
    "96 kbps": 96, "128 kbps": 128, "192 kbps": 192,
    "256 kbps": 256, "320 kbps": 320,
}
CHANNEL_PATH = re.compile(r"^/(?:@[^/]+|(?:channel|c|user)/[^/]+)(?:/(?:videos|playlists))?/?$")
MEDIA_NAME = re.compile(r"\[([^\[\]]+)\]\.(mp4|mp3|m4a|wav)$", re.I)
PROFILE_NAME = re.compile(r" (\[(?:影片|音訊)_[^\[\]]+\]) (?=\[[^\[\]]+\]\.(?:mp4|mp3|m4a|wav)$)", re.I)
CACHE_NAME = ".yt-dlp-cache"


@dataclass
class DownloadOptions:
    url: str
    output: Path
    video: bool = False
    audio: bool = False
    subtitles: bool = True
    categorize: bool = True
    mark_members: bool = True
    subtitle_language: str = "繁體中文"
    auto_subtitles: bool = True
    subtitle_txt: bool = True
    browser: str = "opera"
    cookies_file: str = ""
    audio_format: str = "mp3"
    max_height: int = 0
    video_bitrate: int = 0
    audio_bitrate: int = 0
    reuse_media: bool = True

    def validate(self) -> None:
        parsed = urlparse(self.url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("請輸入完整網址，例如 https://www.youtube.com/watch?v=…")
        if not (self.video or self.audio or self.subtitles):
            raise ValueError("至少勾選影片、音訊或字幕其中一項。")
        if self.subtitle_language not in LANGUAGE_CHOICES:
            raise ValueError("請選擇有效的字幕語言。")
        if self.audio_format not in ("mp3", "m4a", "wav"):
            raise ValueError("不支援的音訊格式。")
        if self.max_height not in (0, 2160, 1080, 720, 480):
            raise ValueError("不支援的影片畫質。")
        if self.video_bitrate not in VIDEO_BITRATE_CHOICES.values():
            raise ValueError("不支援的影片位元率。")
        if self.audio_bitrate not in AUDIO_BITRATE_CHOICES.values():
            raise ValueError("不支援的音訊位元率。")
        if self.browser == "file" and not Path(self.cookies_file).is_file():
            raise ValueError("請選擇存在的 cookies.txt 檔案。")

    @property
    def effective_video_bitrate(self) -> int:
        return 500 if self.video_bitrate == -1 else self.video_bitrate

    @property
    def effective_max_height(self) -> int:
        return min(self.max_height or 480, 480) if self.video_bitrate == -1 else self.max_height

    def media_profile(self, extension: str) -> str:
        if extension == "mp4":
            bitrate, height = self.effective_video_bitrate, self.effective_max_height
            if bitrate or height:
                quality = "省空間_500kbps" if self.video_bitrate == -1 else f"{bitrate}kbps" if bitrate else "來源"
                return f"[影片_{quality}" + (f"_{height}p" if height else "") + "]"
        elif extension in ("mp3", "m4a") and self.audio_bitrate:
            return f"[音訊_{self.audio_bitrate}kbps]"
        return ""


@dataclass
class VideoItem:
    id: str
    url: str
    groups: set[str] = field(default_factory=set)
    member: bool = False


class Cancelled(Exception):
    pass


def app_directory() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def find_tool(name: str, root: Path | None = None) -> str | None:
    executable = name + (".exe" if os.name == "nt" else "")
    root = Path(root) if root is not None else app_directory()
    # 套件管理器更新到 bin；下載時必須優先使用這些本地版本。
    candidates = [root / "bin" / executable, root / executable]
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    candidates += [Path.home() / ".local" / "bin" / executable]
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    candidates.append(local / "Microsoft" / "WinGet" / "Links" / executable)
    if name == "node":
        candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / executable)
    if name in ("ffmpeg", "ffprobe"):
        packages = local / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            candidates += list(packages.glob("Gyan.FFmpeg_*/*/bin/" + executable))
    return next((str(path.resolve()) for path in candidates if path.is_file()), None)


def windows_process_kwargs() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def create_process(command: list[str]) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    # 凍結版的 DLL 搜尋路徑不傳給獨立安裝的 yt-dlp / FFmpeg。
    frozen_windows = os.name == "nt" and getattr(sys, "frozen", False)
    if frozen_windows:
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", env=env, **windows_process_kwargs())
    finally:
        if frozen_windows:
            ctypes.windll.kernel32.SetDllDirectoryW(sys._MEIPASS)


class DownloadEngine:
    def __init__(self, options: DownloadOptions, emit):
        self.options = options
        self.emit = emit
        self.cancelled = threading.Event()
        self.process_lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.processes: list[subprocess.Popen] = []
        self.yt_dlp = find_tool("yt-dlp")
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.node = find_tool("node")
        self.output = options.output.expanduser().resolve()
        self.cache = self.output / CACHE_NAME
        self.processed = self.failed = self.no_subtitles = 0
        self.seen: set[str] = set()
        self.existing: dict[tuple[str, str], Path] = {}
        self.original_videos: dict[str, Path] = {}
        self.quarantined: dict[str, Path] = {}

    def log(self, message: str) -> None:
        self.emit("log", message)

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        self.cancelled.set()
        with self.process_lock:
            for process in self.processes:
                if process.poll() is not None:
                    continue
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=8, **windows_process_kwargs())
                else:
                    process.terminate()

    def common_arguments(self) -> list[str]:
        command = [self.yt_dlp, "--ignore-config", "--encoding", "utf-8", "--no-colors",
                   "--ignore-errors", "--socket-timeout", "20", "--retries", "3"]
        if self.node:
            command += ["--js-runtimes", "node:" + self.node]
        if self.ffmpeg:
            command += ["--ffmpeg-location", str(Path(self.ffmpeg).parent)]
        if self.options.browser == "file":
            command += ["--cookies", self.options.cookies_file]
        elif self.options.browser:
            command += ["--cookies-from-browser", self.options.browser]
        return command

    def run_command(self, command: list[str], on_info=None, on_line=None) -> int:
        self.check_cancelled()
        with self.process_lock:
            self.check_cancelled()
            process = self.process = create_process(command)
            self.processes.append(process)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self.check_cancelled()
                line = line.strip()
                if not line:
                    continue
                if on_line is not None:
                    on_line(line)
                    continue
                if line.startswith("__CURRENT__"):
                    try:
                        self.emit("current", json.loads(line[len("__CURRENT__"):]))
                    except json.JSONDecodeError:
                        pass
                    continue
                if line.startswith("__PROGRESS__"):
                    try:
                        self.emit("progress", json.loads(line[len("__PROGRESS__"):]))
                    except json.JSONDecodeError:
                        pass
                    continue
                json_line = line[len("__VIDEO__"):] if line.startswith("__VIDEO__") else line
                if json_line.startswith("{") and on_info is not None:
                    try:
                        info = json.loads(json_line)
                    except json.JSONDecodeError:
                        self.log("無法解析 yt-dlp 的影片資訊。")
                    else:
                        if isinstance(info, dict):
                            on_info(info)
                    continue
                self.log(line)
            result = process.wait()
            self.check_cancelled()
            return result
        finally:
            with self.process_lock:
                if process.poll() is None:
                    process.terminate()
                self.processes.remove(process)
                self.process = self.processes[-1] if self.processes else None
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()

    def fetch_lists(self, urls: list[str]) -> tuple[list[dict], int]:
        roots: list[dict] = []
        if not urls:
            return roots, 0
        status = self.run_command(self.common_arguments() + [
            "--flat-playlist", "--dump-single-json", "--skip-download", *urls,
        ], roots.append)
        return roots, status

    @staticmethod
    def add_entries(catalog: dict[str, VideoItem], root: dict, group: str = "",
                    owner: str = "") -> None:
        for entry in root.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or "")
            if not key or entry.get("ie_key") == "YoutubeTab":
                continue
            if owner and entry.get("channel_id") and entry["channel_id"] != owner:
                continue
            url = entry.get("webpage_url") or entry.get("url") or ""
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", key):
                url = "https://www.youtube.com/watch?v=" + key
            if not str(url).startswith(("http://", "https://")):
                continue
            item = catalog.setdefault(key, VideoItem(key, url))
            if group:
                item.groups.add(group)
            item.member |= entry.get("availability") == "subscriber_only"

    def discover(self) -> tuple[dict[str, VideoItem], bool]:
        url = self.options.url.strip()
        parsed = urlparse(url)
        youtube = parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")
        query = parse_qs(parsed.query)
        video_id = query.get("v", [""])[0]
        if parsed.hostname == "youtu.be":
            video_id = parsed.path.strip("/")
        if youtube and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return {video_id: VideoItem(video_id, "https://www.youtube.com/watch?v=" + video_id)}, False
        catalog: dict[str, VideoItem] = {}
        if not (youtube and CHANNEL_PATH.fullmatch(parsed.path)):
            roots, status = self.fetch_lists([url])
            for root in roots:
                if root.get("entries") is not None:
                    group = safe_name(root.get("title") or root.get("id") or "播放清單")
                    if group.casefold() == CACHE_NAME.casefold():
                        group += "_播放清單"
                    self.add_entries(catalog, root, group)
                elif root.get("id"):
                    key = str(root["id"])
                    catalog[key] = VideoItem(key, url, member=root.get("availability") == "subscriber_only")
            return catalog, bool(status)

        base_path = re.sub(r"/(videos|playlists)/?$", "", parsed.path).rstrip("/")
        channel = "https://www.youtube.com" + base_path
        self.emit("phase", "正在取得頻道影片與播放清單…")
        urls = [channel + "/videos"]
        if self.options.categorize:
            urls.append(channel + "/playlists")
        roots, status = self.fetch_lists(urls)
        channel_id = next((root.get("channel_id") for root in roots if root.get("channel_id")), "")
        playlists: dict[str, str] = {}
        have_videos = have_playlists = False
        for root in roots:
            source = root.get("original_url") or root.get("webpage_url") or ""
            if urlparse(source).path.rstrip("/").endswith("/playlists"):
                have_playlists = True
                for entry in root.get("entries") or []:
                    if not isinstance(entry, dict):
                        continue
                    key = str(entry.get("id") or "")
                    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
                        playlists[key] = safe_name(entry.get("title") or key)
            else:
                have_videos = True
                self.add_entries(catalog, root)
        counts = Counter(name.casefold() for name in playlists.values())
        groups = {key: name if counts[name.casefold()] == 1 and name.casefold() not in ("未分類", CACHE_NAME)
                  else f"{name[:50]} [{key}]" for key, name in playlists.items()}
        detail_urls = ["https://www.youtube.com/playlist?list=" + key for key in playlists]
        if channel_id.startswith("UC"):
            detail_urls.append("https://www.youtube.com/playlist?list=UUMO" + channel_id[2:])
        self.emit("phase", f"正在整理 {len(playlists)} 個播放清單及會員影片…")
        details, detail_status = self.fetch_lists(detail_urls)
        received = set()
        for root in details:
            key = root.get("id") or ""
            if key in groups:
                received.add(key)
            self.add_entries(catalog, root, groups.get(key, ""), channel_id)
        incomplete = bool(status) or not have_videos or (
            self.options.categorize and (not have_playlists or received != set(groups)))
        if incomplete:
            self.log("部分播放清單無法讀取，分類可能不完整；仍會處理已取得的影片。")
        elif detail_status:
            self.log("會員影片清單未能完整讀取；請確認登入來源與觀看權限。")
        return catalog, incomplete

    def index_existing_media(self) -> None:
        if not self.options.reuse_media:
            return
        for folder, directories, files in os.walk(self.output):
            directories[:] = [name for name in directories if name not in (CACHE_NAME, ".cache")]
            for name in files:
                match = MEDIA_NAME.search(name)
                if match:
                    identity, extension = match[1], match[2].lower()
                    marker = PROFILE_NAME.search(name)
                    profile = marker[1] if marker else ""
                    path = Path(folder) / name
                    if profile == self.options.media_profile(extension):
                        self.existing.setdefault((identity, extension), path)
                    if extension == "mp4" and (not profile or profile.startswith("[影片_來源")):
                        if identity not in self.original_videos or not profile:
                            self.original_videos[identity] = path

    def file_targets(self, info: dict, item: VideoItem, suffix: str) -> list[Path]:
        groups = sorted(item.groups) or ["未分類"]
        folders = [self.output / name for name in groups] if self.options.categorize else [self.output]
        member = item.member or info.get("availability") == "subscriber_only"
        prefix = "[會員] " if self.options.mark_members and member else ""
        identity = safe_name(item.id, 80)
        profile = self.options.media_profile(suffix.lstrip("."))
        profile = " " + profile if profile else ""
        targets = []
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
            remaining = 245 - len(str(folder)) - len(prefix) - len(profile) - len(identity) - len(suffix) - 5
            if remaining < 8:
                raise OSError("輸出路徑太長，請選擇較短的資料夾路徑。")
            title = safe_name(str(info.get("title") or item.id), min(100, remaining))
            target = folder / f"{prefix}{title}{profile} [{identity}]{suffix}"
            if not target.resolve().is_relative_to(self.output):
                raise OSError("分類路徑超出輸出資料夾。")
            targets.append(target)
        return targets

    def publish_media(self, source: Path, targets: list[Path]) -> Path:
        self.check_cancelled()
        if not source.resolve().is_relative_to(self.output):
            raise OSError("影音檔案不在指定的輸出資料夾中。")
        primary = next((target for target in targets if target.resolve() == source.resolve()), targets[0])
        if source.resolve() != primary.resolve():
            if primary.is_file() and self.options.reuse_media:
                primary = source
            else:
                shutil.move(str(source), str(primary))
        for target in targets:
            self.check_cancelled()
            if target.resolve() != primary.resolve():
                if not target.exists() or not self.options.reuse_media:
                    self.copy_media(primary, target)
        return primary

    def copy_media(self, source: Path, target: Path) -> None:
        # 複製到暫存檔後才改名，停止時不會留下被誤認為完整的影音檔。
        temporary = target.with_name(target.name + ".copying")
        with source.open("rb") as incoming, temporary.open("wb") as outgoing:
            while True:
                self.check_cancelled()
                chunk = incoming.read(4 * 1024 * 1024)
                if not chunk:
                    break
                outgoing.write(chunk)
        shutil.copystat(source, temporary)
        self.check_cancelled()
        temporary.replace(target)

    def probe_media(self, source: Path) -> dict:
        lines: list[str] = []
        status = self.run_command([
            self.ffprobe, "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,height,bit_rate:format=duration", "-of", "json", str(source),
        ], on_line=lines.append)
        try:
            info = json.loads("\n".join(lines))
        except ValueError as error:
            raise OSError("無法讀取影音檔案資訊。") from error
        if status:
            raise OSError("影音檔案不完整或無法讀取。")
        return info

    def transcode(self, source: Path, target: Path, arguments: list[str], info: dict) -> None:
        if not target.resolve().is_relative_to(self.cache):
            raise OSError("轉換暫存檔必須位於下載快取資料夾。")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".converting" + target.suffix)
        duration = float((info.get("format") or {}).get("duration") or 0)
        started = time.monotonic()
        progress: dict[str, str] = {}
        self.emit("convert_progress", {"percent": 0})

        def on_line(line: str) -> None:
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[a-zA-Z0-9_]+", key):
                self.log(line)
                return
            progress[key] = value
            if key != "progress":
                return
            try:
                elapsed_media = max(0.0, float(progress.get("out_time_us", "0")) / 1_000_000)
            except ValueError:
                elapsed_media = 0
            percent = 100 if value == "end" else min(99.9, elapsed_media / duration * 100) if duration else 0
            eta = (time.monotonic() - started) * (duration / elapsed_media - 1) if 0 < elapsed_media < duration else None
            self.emit("convert_progress", {"percent": percent, "eta": eta})

        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-nostats", "-progress", "pipe:1", "-i", str(source), *arguments, str(temporary)]
        if self.run_command(command, on_line=on_line):
            raise OSError("FFmpeg 影音轉換失敗，請查看下載記錄。")
        self.check_cancelled()
        temporary.replace(target)

    def extract_audio(self, source: Path, target: Path) -> None:
        self.emit("phase", "正在另存音訊並套用位元率…")
        info = self.probe_media(source)
        audio = next((stream for stream in info.get("streams", []) if stream.get("codec_type") == "audio"), None)
        if audio is None:
            raise OSError("來源檔案沒有音訊串流。")
        bitrate, extension = self.options.audio_bitrate, self.options.audio_format
        if extension == "wav":
            codec = ["-c:a", "pcm_s16le"]
        elif not bitrate and audio.get("codec_name") == {"mp3": "mp3", "m4a": "aac"}[extension]:
            codec = ["-c:a", "copy"]
        elif extension == "mp3":
            codec = ["-c:a", "libmp3lame"] + (["-b:a", f"{bitrate}k"] if bitrate else ["-q:a", "0"])
        else:
            codec = ["-c:a", "aac", "-b:a", f"{bitrate or 320}k"]
        # 指定位元率時一定重新編碼；網站原生 M4A 也不能直接略過此設定。
        self.transcode(source, target, ["-map", "0:a:0", "-vn", *codec], info)

    def compress_video(self, source: Path, target: Path) -> None:
        bitrate, height = self.options.effective_video_bitrate, self.options.effective_max_height
        self.emit("phase", f"正在壓縮影片：{bitrate} kbps…")
        video_filter = f"scale=-2:'trunc(min(ih,{height})/2)*2'" if height else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        audio_bitrate = 64 if self.options.video_bitrate == -1 else 128
        self.transcode(source, target, [
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium",
            "-b:v", f"{bitrate}k", "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k",
            "-vf", video_filter, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_bitrate}k",
            "-movflags", "+faststart",
        ], self.probe_media(source))

    def on_video(self, info: dict, item: VideoItem, mode: str, total: int) -> None:
        self.check_cancelled()
        title = str(info.get("title") or item.id)
        subtitle_count = 0
        try:
            downloads = info.get("requested_downloads") or []
            finished_path = next((download.get("filepath") for download in downloads
                                  if isinstance(download, dict) and download.get("filepath")), "")
            media_path = Path(str(finished_path or info.get("filepath") or ""))
            matching_video = self.existing.get((item.id, "mp4"))
            video_source = media_path if mode == "video" and media_path.is_file() else self.original_videos.get(item.id) or matching_video
            if self.options.video and (not video_source or not video_source.is_file()):
                raise OSError("影片檔案未完整下載。")
            if self.options.video and video_source.suffix.lower() != ".mp4":
                raise OSError("影片尚未完成 MP4 合併或轉封裝。")
            # 音訊先從原始來源取出，避免沿用壓縮影片的低位元率聲音。
            if self.options.audio:
                audio_source = self.existing.get((item.id, self.options.audio_format))
                if not audio_source or not audio_source.is_file():
                    source = media_path if mode == "audio" and media_path.is_file() else video_source
                    if not source or not source.is_file():
                        raise OSError("沒有可用的音訊或影片檔案。")
                    audio_source = self.cache / f"{safe_name(item.id, 80)}.converted.{self.options.audio_format}"
                    self.extract_audio(source, audio_source)
                if audio_source.suffix.lower() != "." + self.options.audio_format:
                    raise OSError("音訊尚未完成指定格式的轉換。")
                self.publish_media(audio_source, self.file_targets(info, item, "." + self.options.audio_format))
                previous = self.quarantined.pop(item.id, None)
                if previous is not None:
                    previous.unlink(missing_ok=True)
            if self.options.video:
                if self.options.video_bitrate and not matching_video:
                    converted = self.cache / f"{safe_name(item.id, 80)}.converted.mp4"
                    self.compress_video(video_source, converted)
                    self.publish_media(converted, self.file_targets(info, item, ".mp4"))
                else:
                    self.publish_media(matching_video or video_source, self.file_targets(info, item, ".mp4"))
            # 只清除本次已成功另存的來源快取；原有完成品與轉換失敗的來源會保留。
            if media_path.is_file() and media_path.resolve().is_relative_to(self.cache) and ".source." in media_path.name:
                media_path.unlink()
            if self.options.subtitles:
                for language, sub in (info.get("requested_subtitles") or {}).items():
                    self.check_cancelled()
                    if not isinstance(sub, dict) or language == "live_chat":
                        continue
                    source = Path(str(sub.get("filepath") or self.cache / f"{safe_name(item.id, 80)}.{language}.vtt"))
                    if not source.resolve().is_relative_to(self.cache) or not source.is_file():
                        raise OSError(f"字幕檔案未完整下載：{language}")
                    targets = self.file_targets(info, item, "." + safe_name(language, 60) + ".vtt")
                    text = ""
                    if self.options.subtitle_txt:
                        automatic = not bool((info.get("subtitles") or {}).get(language))
                        text = vtt_to_text(source.read_text(encoding="utf-8-sig"), automatic=automatic)
                    for target in targets:
                        shutil.copy2(source, target)
                        if self.options.subtitle_txt and text.strip():
                            target.with_suffix(".txt").write_text(text, encoding="utf-8-sig")
                    subtitle_count += 1
                if not subtitle_count:
                    self.no_subtitles += 1
                    available = sorted(set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {}))
                    available = [language for language in available if language != "live_chat"]
                    details = "；可用語言：" + ", ".join(available[:12]) if available else "；網站未提供字幕"
                    self.log("沒有符合所選語言的字幕：" + title + details)
            self.log("已處理：" + title)
        except (OSError, UnicodeError) as error:
            self.failed += 1
            self.log("處理失敗：" + title + " — " + str(error))
        self.seen.add(item.id)
        self.processed += 1
        self.emit("count", {"done": self.processed, "total": total, "title": title})

    def command_for_batch(self, batch: Path, mode: str) -> list[str]:
        command = self.common_arguments() + [
            "--batch-file", str(batch), "--no-playlist", "--windows-filenames",
            "--no-simulate", "--no-quiet", "--progress", "--newline", "--progress-delta", "0.4",
            "--output", str(self.cache / "%(id)s.source.%(ext)s"),
            "--progress-template", "download:__PROGRESS__%(progress)j",
            "--print", "before_dl:__CURRENT__%(title)j",
            "--print", "after_video:__VIDEO__%()j",
            "--post-overwrites",
        ]
        command.append("--no-overwrites" if self.options.reuse_media else "--force-overwrites")
        if mode == "video":
            command += ["-t", "mp4"]
            if self.options.effective_max_height:
                height = str(self.options.effective_max_height)
                command += ["-f", f"bv*[height<={height}]+ba/b[height<={height}]"]
        elif mode == "audio":
            # 先取得純音訊來源，再統一用 FFmpeg 套用格式及位元率。
            command += ["-f", "bestaudio/best"]
        else:
            command += ["--skip-download"]
        if self.options.subtitles:
            command += ["--write-subs", "--sub-format", "vtt",
                        "--sub-langs", LANGUAGE_CHOICES[self.options.subtitle_language]]
            if self.options.auto_subtitles:
                command.append("--write-auto-subs")
        return command

    def execute(self) -> dict:
        self.options.validate()
        if not self.yt_dlp:
            raise RuntimeError("找不到 yt-dlp.exe。可放在工具旁邊、bin 資料夾，或加入 PATH。")
        if (self.options.video or self.options.audio) and (not self.ffmpeg or not self.ffprobe):
            raise RuntimeError("下載影音需要 FFmpeg。請將 ffmpeg.exe / ffprobe.exe 放在工具旁邊或加入 PATH。")
        self.output.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.emit("phase", "正在讀取網址…")
        catalog, incomplete = self.discover()
        if not catalog:
            raise RuntimeError("沒有取得可下載的影片，請查看記錄中的網址或登入錯誤。")
        self.index_existing_media()
        if self.options.video:
            quality = next(label for label, value in VIDEO_BITRATE_CHOICES.items() if value == self.options.video_bitrate)
            self.log("影片位元率：" + quality)
        if self.options.audio:
            quality = next(label for label, value in AUDIO_BITRATE_CHOICES.items() if value == self.options.audio_bitrate)
            self.log("音訊位元率：" + ("WAV 未壓縮，不套用位元率" if self.options.audio_format == "wav" else quality))
        total = len(catalog)
        self.emit("count", {"done": 0, "total": total, "title": ""})
        self.log(f"共 {total} 支影片。相同影片會下載一次，再放到所屬的各個播放清單。")
        batches: dict[str, list[VideoItem]] = {"video": [], "audio": [], "metadata": []}
        for item in catalog.values():
            has_video = (item.id, "mp4") in self.existing
            has_audio = (item.id, self.options.audio_format) in self.existing
            has_source = item.id in self.original_videos or (has_video and not self.options.video_bitrate)
            if self.options.video and not has_video and (not self.options.video_bitrate or not has_source):
                mode = "video"
            elif self.options.audio and not has_audio and not has_source:
                mode = "audio"
            else:
                mode = "metadata"
            batches[mode].append(item)
        for mode, items in batches.items():
            if not items:
                continue
            self.check_cancelled()
            if mode == "audio":
                for item in items:
                    # 完成品已搬到分類資料夾；仍在快取中的轉換檔沒有完成確認，
                    # 不讓 yt-dlp 用它直接跳過來源下載與轉換。
                    pending = self.cache / f"{safe_name(item.id, 80)}.converted.{self.options.audio_format}"
                    if pending.is_file():
                        parked = pending.with_name(pending.name + ".unfinished")
                        pending.replace(parked)
                        self.quarantined[item.id] = parked
            self.emit("phase", {"video": "正在下載影片…", "audio": "正在下載音訊…",
                                "metadata": "正在處理字幕與已有檔案…"}[mode])
            batch_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".urls",
                                                 dir=self.cache, delete=False) as batch:
                    batch_path = Path(batch.name)
                    for item in items:
                        batch.write(item.url + "\n")
                lookup = {item.id: item for item in items}

                def on_info(info):
                    item = lookup.get(str(info.get("id") or ""))
                    if item:
                        self.on_video(info, item, mode, total)

                status = self.run_command(self.command_for_batch(batch_path, mode), on_info)
                if status:
                    incomplete = True
                    self.log("部分項目下載失敗，其他項目會繼續處理。")
            finally:
                if batch_path is not None:
                    batch_path.unlink(missing_ok=True)
        self.failed += len(set(catalog) - self.seen)
        return {"total": total, "processed": self.processed, "failed": self.failed,
                "no_subtitles": self.no_subtitles, "incomplete": bool(incomplete or self.failed), "output": str(self.output)}
