"""檢查及更新本地 yt-dlp / FFmpeg；Node.js 由使用者執行官方安裝程式。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile

from download_engine import Cancelled, app_directory, create_process, find_tool


YTDLP_RELEASE_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
FFMPEG_VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
FFMPEG_PACKAGE_BASE = "https://www.gyan.dev/ffmpeg/builds/packages/"
NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
NODE_MINIMUM_VERSION = (22, 0, 0)
TOOL_LABELS = {"yt-dlp": "yt-dlp", "ffmpeg": "FFmpeg", "ffprobe": "ffprobe", "node": "Node.js"}


@dataclass
class LocalTool:
    name: str
    path: str = ""
    version: str = ""
    error: str = ""


@dataclass
class Release:
    version: str
    url: str
    sha256: str


def version_key(version: str) -> tuple[int, ...]:
    match = re.match(r"v?(\d+(?:\.\d+)+)", version)
    if not match:
        return ()
    return tuple(int(part) for part in match[1].split("."))


class DependencyManager:
    def __init__(self, emit, root: Path | None = None):
        self.root = Path(root or app_directory()).resolve()
        self.directory = (self.root / "bin").resolve()
        if not self.directory.is_relative_to(self.root):
            raise OSError("本地 bin 資料夾不可指向工具資料夾以外的位置。")
        self.emit = emit
        self.cancelled = threading.Event()
        self.process_lock = threading.Lock()
        self.process = None
        self.tools: dict[str, LocalTool] = {}
        self.errors: list[str] = []

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        self.cancelled.set()
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()

    def status(self, name: str, state: str, latest: str = "") -> None:
        local = self.tools.get(name, LocalTool(name))
        self.emit("status", {"name": name, "current": local.version or "未找到",
                             "latest": latest, "state": state, "path": local.path})

    def read_version(self, name: str, path: str) -> str:
        self.check_cancelled()
        command = [path, "--version" if name in ("yt-dlp", "node") else "-version"]
        with self.process_lock:
            self.check_cancelled()
            process = self.process = create_process(command)
        deadline = time.monotonic() + 12
        try:
            while True:
                self.check_cancelled()
                try:
                    output, _ = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise OSError(TOOL_LABELS[name] + " 版本檢查逾時。")
            self.check_cancelled()
            patterns = {
                "yt-dlp": r"(?m)^(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)\s*$",
                "ffmpeg": r"(?m)^ffmpeg version (\S+)",
                "ffprobe": r"(?m)^ffprobe version (\S+)",
                "node": r"(?m)^v(\d+\.\d+\.\d+)\s*$",
            }
            match = re.search(patterns[name], output)
            if process.returncode or not match:
                raise OSError(TOOL_LABELS[name] + " 無法執行或版本資訊無法辨識。")
            version = match[1]
            clean = re.match(r"(\d+(?:\.\d+)+)", version)
            return clean[1] if clean else version
        finally:
            with self.process_lock:
                if process.poll() is None:
                    process.kill()
                self.process = None
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

    def inspect_local(self, name: str) -> LocalTool:
        path = find_tool(name, root=self.root)
        if not path:
            return LocalTool(name)
        try:
            return LocalTool(name, path, self.read_version(name, path))
        except OSError as error:
            return LocalTool(name, path, error=str(error))

    def open_url(self, url: str):
        self.check_cancelled()
        if not url.startswith("https://"):
            raise OSError("套件下載網址必須使用 HTTPS。")
        request = urllib.request.Request(url, headers={"User-Agent": "LQMAMA-Downloader/1.0"})
        return urllib.request.urlopen(request, timeout=20)

    def fetch_text(self, url: str, limit: int = 2 * 1024 * 1024) -> str:
        with self.open_url(url) as response:
            data = response.read(limit + 1)
        self.check_cancelled()
        if len(data) > limit:
            raise OSError("套件版本資料超過大小限制。")
        return data.decode("utf-8-sig")

    def ytdlp_release(self) -> Release:
        metadata = json.loads(self.fetch_text(YTDLP_RELEASE_API))
        version = metadata.get("tag_name", "")
        if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?", version):
            raise OSError("yt-dlp 最新版本資料無法辨識。")
        assets = {asset["name"]: asset for asset in metadata.get("assets", [])}
        asset = assets.get("yt-dlp.exe")
        if not asset:
            raise OSError("yt-dlp 發行版本沒有 Windows 執行檔。")
        expected_url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{version}/yt-dlp.exe"
        if asset.get("browser_download_url") != expected_url:
            raise OSError("yt-dlp 下載網址與官方發行位置不符。")
        digest = asset.get("digest") or ""
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            checksum = digest.split(":", 1)[1].lower()
        else:
            sums = self.fetch_text(f"https://github.com/yt-dlp/yt-dlp/releases/download/{version}/SHA2-256SUMS")
            found = re.search(r"(?m)^([0-9a-fA-F]{64})\s+\*?yt-dlp\.exe\s*$", sums)
            if not found:
                raise OSError("yt-dlp 官方資料缺少 SHA-256 校驗碼。")
            checksum = found[1].lower()
        return Release(version, expected_url, checksum)

    def ffmpeg_release(self) -> Release:
        version = self.fetch_text(FFMPEG_VERSION_URL, 1024).strip()
        if not re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
            raise OSError("FFmpeg 最新版本資料無法辨識。")
        url = FFMPEG_PACKAGE_BASE + f"ffmpeg-{version}-essentials_build.zip"
        found = re.match(r"([0-9a-fA-F]{64})(?:\s|$)", self.fetch_text(url + ".sha256", 4096).strip())
        if not found:
            raise OSError("FFmpeg 官方建置缺少有效的 SHA-256 校驗碼。")
        return Release(version, url, found[1].lower())

    def download(self, name: str, release: Release, target: Path) -> None:
        self.status(name, "下載中", release.version)
        self.emit("log", f"正在下載 {TOOL_LABELS[name]} {release.version}：{release.url}")
        digest = hashlib.sha256()
        downloaded = 0
        last_update = 0.0
        with self.open_url(release.url) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            if total > 512 * 1024 * 1024:
                raise OSError("套件下載檔案超過大小限制。")
            while True:
                self.check_cancelled()
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > 512 * 1024 * 1024:
                    raise OSError("套件下載檔案超過大小限制。")
                output.write(chunk)
                digest.update(chunk)
                if time.monotonic() - last_update >= 0.25:
                    self.emit("progress", {"name": name, "downloaded": downloaded, "total": total})
                    last_update = time.monotonic()
        self.check_cancelled()
        if total and downloaded != total:
            raise OSError("套件下載不完整，請重新檢查套件。")
        if digest.hexdigest() != release.sha256:
            raise OSError("套件 SHA-256 校驗失敗；保留原有套件，請重新檢查。")
        self.emit("progress", {"name": name, "downloaded": downloaded, "total": downloaded})

    @contextmanager
    def staging(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".套件更新-", dir=self.directory)).resolve()
        if not stage.is_relative_to(self.directory):
            raise OSError("套件暫存路徑超出本地資料夾。")
        try:
            yield stage
        finally:
            if (stage / ".keep-backup").exists():
                self.emit("log", "原有套件備份已保留在：" + str(stage))
            elif stage.exists():
                # 僅刪除本次建立且已確認位於 bin 中的暫存資料夾。
                if stage.resolve().is_relative_to(self.directory) and stage.name.startswith(".套件更新-"):
                    shutil.rmtree(stage)

    def validate_binary(self, name: str, path: Path, version: str) -> None:
        with path.open("rb") as source:
            if source.read(2) != b"MZ":
                raise OSError(TOOL_LABELS[name] + " 不是有效的 Windows 執行檔。")
        actual = self.read_version(name, str(path))
        if version_key(actual) != version_key(version):
            raise OSError(f"{TOOL_LABELS[name]} 下載版本不符：{actual}，預期 {version}。")

    def publish(self, files: dict[str, Path], stage: Path) -> None:
        self.check_cancelled()
        backups: dict[Path, Path] = {}
        published: list[Path] = []
        try:
            for filename, source in files.items():
                target = self.directory / filename
                if target.name != filename or not target.resolve().is_relative_to(self.directory):
                    raise OSError("套件檔案路徑超出本地資料夾。")
                if target.exists():
                    backup = stage / (filename + ".backup")
                    os.replace(target, backup)
                    backups[target] = backup
                os.replace(source, target)
                published.append(target)
        except OSError:
            restore_failed = False
            for target in reversed(published):
                try:
                    target.unlink()
                except OSError:
                    restore_failed = True
            for target, backup in backups.items():
                try:
                    os.replace(backup, target)
                except OSError:
                    restore_failed = True
            if restore_failed:
                (stage / ".keep-backup").touch()
            raise OSError("套件更新無法寫入，請關閉使用這些套件的程式後重試。")

    def install_ytdlp(self, release: Release) -> None:
        with self.staging() as stage:
            target = stage / "yt-dlp.exe"
            self.download("yt-dlp", release, target)
            self.status("yt-dlp", "驗證中", release.version)
            self.validate_binary("yt-dlp", target, release.version)
            self.publish({"yt-dlp.exe": target}, stage)

    def extract_ffmpeg(self, package: Path, stage: Path) -> dict[str, Path]:
        files: dict[str, Path] = {}
        with zipfile.ZipFile(package) as archive:
            for member in archive.infolist():
                self.check_cancelled()
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
                    raise OSError("FFmpeg 壓縮檔含有不安全的路徑。")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise OSError("FFmpeg 壓縮檔不可包含符號連結。")
                filename = path.name.lower()
                if filename not in ("ffmpeg.exe", "ffprobe.exe"):
                    continue
                if len(path.parts) < 2 or path.parts[-2].lower() != "bin" or filename in files:
                    raise OSError("FFmpeg 壓縮檔的執行檔位置不正確。")
                if member.file_size > 256 * 1024 * 1024:
                    raise OSError("FFmpeg 執行檔超過大小限制。")
                target = stage / filename
                with archive.open(member) as source, target.open("wb") as output:
                    while chunk := source.read(256 * 1024):
                        self.check_cancelled()
                        output.write(chunk)
                files[filename] = target
            for member in archive.infolist():
                if PurePosixPath(member.filename).name.lower() == "license" and member.file_size < 1024 * 1024:
                    target = stage / "FFmpeg-LICENSE.txt"
                    target.write_bytes(archive.read(member))
                    files[target.name] = target
                    break
        if not {"ffmpeg.exe", "ffprobe.exe"}.issubset(files):
            raise OSError("FFmpeg 壓縮檔沒有完整的 ffmpeg.exe / ffprobe.exe。")
        return files

    def install_ffmpeg(self, release: Release) -> None:
        with self.staging() as stage:
            package = stage / "ffmpeg.zip"
            self.download("ffmpeg", release, package)
            self.status("ffmpeg", "解壓縮及驗證中", release.version)
            self.status("ffprobe", "解壓縮及驗證中", release.version)
            files = self.extract_ffmpeg(package, stage)
            for name in ("ffmpeg", "ffprobe"):
                self.validate_binary(name, files[name + ".exe"], release.version)
            self.publish(files, stage)

    def check_and_update(self) -> dict:
        self.emit("log", "本地套件資料夾：" + str(self.directory))
        for name in TOOL_LABELS:
            self.check_cancelled()
            self.tools[name] = self.inspect_local(name)
            self.status(name, "等待檢查" if self.tools[name].version else "缺少或無法執行")
            if self.tools[name].path:
                self.emit("log", f"{TOOL_LABELS[name]}：{self.tools[name].version or self.tools[name].error}；{self.tools[name].path}")
        node_required = version_key(self.tools["node"].version) < NODE_MINIMUM_VERSION
        self.status("node", "請安裝 Node.js" if node_required else "已安裝", "22.0.0 以上")
        for name, get_release, install, members in (
            ("yt-dlp", self.ytdlp_release, self.install_ytdlp, ("yt-dlp",)),
            ("ffmpeg", self.ffmpeg_release, self.install_ffmpeg, ("ffmpeg", "ffprobe")),
        ):
            self.check_cancelled()
            try:
                for member in members:
                    self.status(member, "查詢最新版本")
                release = get_release()
                keys = [version_key(self.tools[member].version) for member in members]
                local_files = all(Path(self.tools[member].path).resolve().is_relative_to(self.root) for member in members)
                current = local_files and all(key and key >= version_key(release.version) for key in keys) and len(set(keys)) == 1
                if not current:
                    install(release)
                    for member in members:
                        self.tools[member] = LocalTool(member, str(self.directory / (member + ".exe")), release.version)
                for member in members:
                    self.status(member, "已是最新版" if current else "已更新至本地資料夾", release.version)
                if not current:
                    self.emit("log", f"{TOOL_LABELS[name]} {release.version} 已安裝至 {self.directory}")
            except Cancelled:
                raise
            except Exception as error:
                message = TOOL_LABELS[name] + " 檢查或更新失敗：" + str(error)
                self.errors.append(message)
                self.emit("log", message)
                for member in members:
                    self.status(member, "檢查或更新失敗")
        self.check_cancelled()
        return {"errors": self.errors, "node_required": node_required,
                "versions": {name: tool.version for name, tool in self.tools.items()}}
