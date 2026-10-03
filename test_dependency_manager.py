"""套件更新測試：使用小型離線下載資料，不修改電腦上的安裝。"""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import dependency_manager as packages
from download_engine import Cancelled, find_tool


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class DependencyTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="_packages_test_", dir=workspace)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.assertTrue(self.directory.is_relative_to(workspace))
        self.root = self.directory / "app"
        self.root.mkdir()
        self.events = []
        self.manager = packages.DependencyManager(lambda kind, value: self.events.append((kind, value)), root=self.root)
        self.ytdlp_data = b"MZyt-dlp:2026.10.01"
        self.ffmpeg_data = self.zip_data()
        self.urls = {}
        self.set_remote_data()
        self.manager.open_url = lambda url: Response(self.urls[url])
        self.manager.read_version = lambda name, path: Path(path).read_bytes().decode().split(":", 1)[1]
        self.manager.inspect_local = self.inspect_fixture

    @staticmethod
    def zip_data(extra=None, missing_probe=False, probe_version="9.0.2"):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("ffmpeg-test/bin/ffmpeg.exe", "MZffmpeg:9.0.2")
            if not missing_probe:
                archive.writestr("ffmpeg-test/bin/ffprobe.exe", "MZffprobe:" + probe_version)
            archive.writestr("ffmpeg-test/LICENSE", "Fixture license")
            if extra:
                archive.writestr(extra, "unsafe data")
        return data.getvalue()

    def set_remote_data(self):
        yt_url = "https://github.com/yt-dlp/yt-dlp/releases/download/2026.10.01/yt-dlp.exe"
        ff_url = packages.FFMPEG_PACKAGE_BASE + "ffmpeg-9.0.2-essentials_build.zip"
        self.urls = {
            packages.YTDLP_RELEASE_API: json.dumps({"tag_name": "2026.10.01", "assets": [{
                "name": "yt-dlp.exe", "browser_download_url": yt_url,
                "digest": "sha256:" + hashlib.sha256(self.ytdlp_data).hexdigest(),
            }]}).encode(),
            yt_url: self.ytdlp_data,
            packages.FFMPEG_VERSION_URL: b"9.0.2",
            ff_url: self.ffmpeg_data,
            ff_url + ".sha256": hashlib.sha256(self.ffmpeg_data).hexdigest().encode(),
        }

    def inspect_fixture(self, name):
        if name == "node":
            return packages.LocalTool(name, version="24.0.0")
        path = self.manager.directory / (name + ".exe")
        if not path.is_file():
            path = self.root / (name + ".exe")
        if path.is_file():
            return packages.LocalTool(name, str(path), self.manager.read_version(name, str(path)))
        return packages.LocalTool(name)

    def test_missing_packages_install_pair_and_prefer_local_updated_files(self):
        old = self.root / "yt-dlp.exe"
        old.write_bytes(b"MZyt-dlp:2026.01.01")
        result = self.manager.check_and_update()
        self.assertFalse(result["errors"], self.events)
        self.assertFalse(result["node_required"])
        self.assertEqual(old.read_bytes(), b"MZyt-dlp:2026.01.01")
        self.assertEqual((self.manager.directory / "yt-dlp.exe").read_bytes(), self.ytdlp_data)
        self.assertEqual((self.manager.directory / "ffmpeg.exe").read_bytes(), b"MZffmpeg:9.0.2")
        self.assertEqual((self.manager.directory / "ffprobe.exe").read_bytes(), b"MZffprobe:9.0.2")
        self.assertTrue((self.manager.directory / "FFmpeg-LICENSE.txt").is_file())
        self.assertEqual(Path(find_tool("yt-dlp", root=self.root)), self.manager.directory / "yt-dlp.exe")
        self.assertFalse(list(self.manager.directory.glob(".套件更新-*")))
        self.assertTrue(any(kind == "progress" for kind, _ in self.events))

    def test_up_to_date_local_versions_skip_download_and_old_node_requires_installer(self):
        self.assertFalse(self.manager.check_and_update()["errors"])
        with patch.object(self.manager, "download", side_effect=AssertionError("已更新的本地套件不應重複下載")):
            result = self.manager.check_and_update()
        self.assertFalse(result["errors"])
        normal_inspect = self.manager.inspect_local
        self.manager.inspect_local = lambda name: packages.LocalTool(name, version="20.0.0") if name == "node" else normal_inspect(name)
        self.assertTrue(self.manager.check_and_update()["node_required"])

    def test_system_versions_are_copied_to_local_by_official_download(self):
        system = self.directory / "system"
        system.mkdir()
        normal_inspect = self.manager.inspect_local
        self.manager.inspect_local = lambda name: packages.LocalTool(name, str(system / (name + ".exe")),
                                                                     "2026.10.01" if name == "yt-dlp" else "9.0.2") if name != "node" else normal_inspect(name)
        self.assertFalse(self.manager.check_and_update()["errors"])
        self.assertTrue((self.manager.directory / "yt-dlp.exe").is_file())
        self.assertTrue((self.manager.directory / "ffmpeg.exe").is_file())

    def test_checksum_failure_keeps_existing_executable(self):
        self.manager.directory.mkdir()
        original = self.manager.directory / "yt-dlp.exe"
        original.write_bytes(b"MZyt-dlp:2026.01.01")
        self.ytdlp_data = b"MZdownload corrupted"
        self.urls["https://github.com/yt-dlp/yt-dlp/releases/download/2026.10.01/yt-dlp.exe"] = self.ytdlp_data
        result = self.manager.check_and_update()
        self.assertTrue(any("SHA-256" in error for error in result["errors"]))
        self.assertEqual(original.read_bytes(), b"MZyt-dlp:2026.01.01")
        self.assertFalse(list(self.manager.directory.glob(".套件更新-*")))

    def test_bad_zip_paths_or_missing_companion_do_not_publish(self):
        for extra, missing in (("../outside.txt", False), (None, True)):
            with self.subTest(extra=extra, missing=missing):
                self.ffmpeg_data = self.zip_data(extra, missing)
                self.set_remote_data()
                with self.assertRaises(OSError):
                    self.manager.install_ffmpeg(self.manager.ffmpeg_release())
                self.assertFalse((self.manager.directory / "ffmpeg.exe").exists())
                self.assertFalse((self.root / "outside.txt").exists())
                self.assertFalse(list(self.manager.directory.glob(".套件更新-*")))

    def test_version_mismatch_keeps_old_ffmpeg_pair(self):
        self.manager.directory.mkdir()
        for name in ("ffmpeg", "ffprobe"):
            (self.manager.directory / (name + ".exe")).write_bytes(("MZ" + name + ":8.1.1").encode())
        self.ffmpeg_data = self.zip_data(probe_version="8.1.1")
        self.set_remote_data()
        with self.assertRaisesRegex(OSError, "下載版本不符"):
            self.manager.install_ffmpeg(self.manager.ffmpeg_release())
        for name in ("ffmpeg", "ffprobe"):
            self.assertEqual((self.manager.directory / (name + ".exe")).read_bytes(), ("MZ" + name + ":8.1.1").encode())

    def test_pair_publish_failure_rolls_back_both_executables(self):
        self.manager.directory.mkdir()
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (self.manager.directory / name).write_bytes(b"old " + name.encode())
        real_replace = packages.os.replace
        with self.manager.staging() as stage:
            files = {}
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                files[name] = stage / name
                files[name].write_bytes(b"new " + name.encode())

            def fail_second_replace(source, target):
                if Path(source) == files["ffprobe.exe"]:
                    raise OSError("fixture file locked")
                return real_replace(source, target)

            with patch.object(packages.os, "replace", side_effect=fail_second_replace):
                with self.assertRaises(OSError):
                    self.manager.publish(files, stage)
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            self.assertEqual((self.manager.directory / name).read_bytes(), b"old " + name.encode())
        self.assertFalse(list(self.manager.directory.glob(".套件更新-*")))

    def test_cancel_mid_download_keeps_old_version_and_removes_partial(self):
        self.manager.directory.mkdir()
        old = self.manager.directory / "yt-dlp.exe"
        old.write_bytes(b"old complete exe")
        release = self.manager.ytdlp_release()
        manager = self.manager

        class InterruptedResponse(Response):
            def read(self, length=-1):
                chunk = super().read(length)
                manager.cancelled.set()
                return chunk

        self.manager.open_url = lambda url: InterruptedResponse(self.ytdlp_data)
        with self.assertRaises(Cancelled):
            self.manager.install_ytdlp(release)
        self.assertEqual(old.read_bytes(), b"old complete exe")
        self.assertFalse(list(self.manager.directory.glob(".套件更新-*")))

    def test_real_installed_tool_version_commands(self):
        manager = packages.DependencyManager(lambda *event: None, root=self.root)
        for name in packages.TOOL_LABELS:
            path = find_tool(name)
            if path:
                self.assertTrue(packages.version_key(manager.read_version(name, path)), name)

    def test_gui_background_check_and_node_installer_link(self):
        import tkinter as tk
        import downloader_ui
        gate = threading.Event()

        class FixtureManager:
            def __init__(self, emit, root=None):
                self.emit = emit

            def check_and_update(self):
                for name in packages.TOOL_LABELS:
                    self.emit("status", {"name": name, "current": "未找到", "latest": "—", "state": "測試檢查中"})
                self.emit("progress", {"name": "ffmpeg", "downloaded": 50, "total": 100})
                gate.wait(5)
                return {"errors": [], "node_required": True}

            def cancel(self):
                gate.set()

        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(downloader_ui, "app_directory", return_value=self.root):
                app = downloader_ui.DownloaderApp(root)
                with patch.object(downloader_ui, "DependencyManager", FixtureManager), patch.object(downloader_ui.webbrowser, "open", return_value=True) as open_browser:
                    app.check_dependencies()
                    self.assertTrue(app.dependencies_running)
                    self.assertIn("disabled", app.start_button.state())
                    self.assertIn("disabled", app.package_button.state())
                    deadline = time.monotonic() + 2
                    while app.dependency_bar["value"] != 50 and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.02)
                    self.assertEqual(app.dependency_bar["value"], 50)
                    gate.set()
                    deadline = time.monotonic() + 5
                    while app.dependencies_running and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.02)
                    self.assertFalse(app.dependencies_running)
                    self.assertNotIn("disabled", app.start_button.state())
                    self.assertNotIn("disabled", app.package_button.state())
                    self.assertNotIn("disabled", app.node_install_button.state())
                    open_browser.assert_called_once_with(packages.NODE_DOWNLOAD_URL)
                    self.assertIn("再次檢查", app.dependency_message.get())
        finally:
            gate.set()
            for event in root.tk.call("after", "info"):
                root.after_cancel(event)
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
