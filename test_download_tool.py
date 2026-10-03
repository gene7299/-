"""離線測試：使用本機影音與字幕，不下載網站影片。"""

import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import download_engine as backend


class DownloadToolTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="_gui_test_", dir=self.workspace)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(self.workspace))
        self.addCleanup(self.temporary.cleanup)
        self.video_id = "testVideo01"
        self.events = []
        self.ffmpeg = backend.find_tool("ffmpeg")
        self.assertIsNotNone(self.ffmpeg)
        self.media = self.root / "source.mp4"
        made = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.3", "-c:v", "mpeg4",
            "-c:a", "aac", "-y", str(self.media),
        ], capture_output=True, timeout=20, **backend.windows_process_kwargs())
        self.assertEqual(made.returncode, 0, made.stderr)
        self.subtitle = self.root / "source.vtt"
        self.subtitle.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.300\n<b>測試字幕</b> &amp; 中文\n", encoding="utf-8")
        self.fixture = self.root / "fixture.info.json"
        self.fixture_info = {
            "id": self.video_id, "title": "離線:影片/測試", "availability": "subscriber_only",
            "extractor": "youtube", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=" + self.video_id,
            "formats": [{"url": self.media.as_uri(), "format_id": "fixture", "ext": "mp4",
                         "width": 160, "height": 90, "vcodec": "mp4v", "acodec": "aac"}],
            "subtitles": {"zh-Hant": [{"url": self.subtitle.as_uri(), "ext": "vtt"}],
                          "zh": [{"url": self.subtitle.as_uri(), "ext": "vtt"}],
                          "en": [{"url": self.subtitle.as_uri(), "ext": "vtt"}]},
        }
        self.fixture.write_text(json.dumps(self.fixture_info, ensure_ascii=False), encoding="utf-8")

    def make_engine(self, folder, **kwargs):
        options = backend.DownloadOptions(url=self.fixture_info["webpage_url"], output=self.root / folder,
                                          browser="", **kwargs)
        engine = backend.DownloadEngine(options, lambda kind, value: self.events.append((kind, value)))
        common = engine.common_arguments() + ["--enable-file-urls", "--load-info-json", str(self.fixture)]
        engine.common_arguments = lambda: common
        item = backend.VideoItem(self.video_id, options.url, {"清單一", "清單二"})
        engine.discover = lambda: ({self.video_id: item}, False)
        return engine

    def probe(self, source):
        result = subprocess.run([
            backend.find_tool("ffprobe"), "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,height,bit_rate", "-of", "json", str(source),
        ], capture_output=True, text=True, encoding="utf-8", timeout=15, **backend.windows_process_kwargs())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["streams"]

    def test_audio_bitrates_apply_to_native_m4a_and_reduce_size(self):
        source = self.root / "native.m4a"
        made = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "anoisesrc=color=pink:sample_rate=48000:duration=4", "-ac", "2", "-c:a", "aac",
            "-b:a", "256k", "-y", str(source),
        ], capture_output=True, timeout=20, **backend.windows_process_kwargs())
        self.assertEqual(made.returncode, 0, made.stderr)
        self.fixture_info["formats"] = [{"url": source.as_uri(), "format_id": "nativeAAC", "ext": "m4a",
                                         "vcodec": "none", "acodec": "mp4a.40.2"}]
        self.fixture.write_text(json.dumps(self.fixture_info), encoding="utf-8")
        for extension in ("mp3", "m4a"):
            sizes = []
            for bitrate in (64, 128, 320):
                with self.subTest(extension=extension, bitrate=bitrate):
                    engine = self.make_engine("bitrates_" + extension, audio=True, subtitles=False,
                                              audio_format=extension, audio_bitrate=bitrate)
                    self.assertEqual(engine.execute()["failed"], 0, self.events[-12:])
                    target = next((engine.output / "清單一").glob(f"*音訊_{bitrate}kbps*.{extension}"))
                    streams = self.probe(target)
                    self.assertEqual([stream["codec_type"] for stream in streams], ["audio"])
                    actual = int(streams[0]["bit_rate"])
                    self.assertAlmostEqual(actual, bitrate * 1000, delta=bitrate * 1000 * 0.20)
                    sizes.append(target.stat().st_size)
                    self.assertFalse(list(engine.cache.glob("*.source.*")))
            self.assertEqual(sizes, sorted(sizes))
            self.assertLess(sizes[0], sizes[2] * 0.4)
        highest = self.make_engine("native_highest", audio=True, subtitles=False, audio_format="m4a")
        self.assertEqual(highest.execute()["failed"], 0, self.events[-12:])
        target = next((highest.output / "清單一").glob("*.m4a"))
        hashes = []
        for path in (source, target):
            result = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0",
                                     "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"],
                                    capture_output=True, timeout=15, **backend.windows_process_kwargs())
            self.assertEqual(result.returncode, 0, result.stderr)
            hashes.append(result.stdout)
        self.assertEqual(*hashes, "最高品質的原生 AAC 應保留音訊資料，不重新編碼")

    def test_video_bitrate_presets_reuse_and_separate_audio_quality(self):
        made = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=854x600:rate=20",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3", "-c:v", "libx264",
            "-preset", "fast", "-crf", "18", "-c:a", "aac", "-b:a", "256k", "-ac", "2", "-y", str(self.media),
        ], capture_output=True, timeout=25, **backend.windows_process_kwargs())
        self.assertEqual(made.returncode, 0, made.stderr)
        self.fixture_info["formats"][0].update(width=854, height=600, vcodec="avc1", acodec="mp4a.40.2")
        self.fixture.write_text(json.dumps(self.fixture_info), encoding="utf-8")
        highest = self.make_engine("video_bitrates", video=True, subtitles=False)
        original_data = self.media.read_bytes()
        self.assertEqual(highest.execute()["failed"], 0, self.events[-12:])
        original = next((highest.output / "清單一").glob("*.mp4"))
        self.assertEqual(original.read_bytes(), original_data)
        self.media.unlink()  # 改變位元率要從已下載的來源轉換，不能忽略設定或再下載影片。
        smallest = self.make_engine("video_bitrates", video=True, audio=True, subtitles=False,
                                    video_bitrate=-1, audio_format="m4a")
        self.assertEqual(smallest.execute()["failed"], 0, self.events[-12:])
        video = next((smallest.output / "清單一").glob("*省空間*.mp4"))
        streams = self.probe(video)
        video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
        self.assertEqual(video_stream["codec_name"], "h264")
        self.assertEqual(video_stream["height"], 480)
        self.assertLess(int(video_stream["bit_rate"]), 650_000)
        self.assertLess(video.stat().st_size, original.stat().st_size)
        saved_audio = next((smallest.output / "清單一").glob("*.m4a"))
        source_audio = next(stream for stream in self.probe(original) if stream["codec_type"] == "audio")
        self.assertEqual(self.probe(saved_audio)[0]["bit_rate"], source_audio["bit_rate"],
                         "獨立音訊必須沿用原來源，不能從 64 kbps 的省空間影片再擷取")
        fixed = self.make_engine("video_bitrates", video=True, subtitles=False, video_bitrate=2000)
        self.assertEqual(fixed.execute()["failed"], 0, self.events[-12:])
        target = next((fixed.output / "清單一").glob("*影片_2000kbps*.mp4"))
        stream = next(stream for stream in self.probe(target) if stream["codec_type"] == "video")
        self.assertEqual(stream["height"], 600)
        self.assertLess(int(stream["bit_rate"]), 2_600_000)
        self.assertGreater(target.stat().st_size, video.stat().st_size)
        again = self.make_engine("video_bitrates", video=True, subtitles=False, video_bitrate=-1)
        with patch.object(again, "compress_video", side_effect=AssertionError("相同品質不應重複壓縮")):
            self.assertEqual(again.execute()["failed"], 0, self.events[-12:])
        self.assertEqual(len(list((again.output / "清單一").glob("*.mp4"))), 3)
        self.assertEqual(original.read_bytes(), original_data)
        self.assertFalse(list(again.cache.glob("*.mp4")))
        self.assertTrue(any(kind == "convert_progress" for kind, _ in self.events))

    def test_all_download_combinations(self):
        for video, audio, subtitles in itertools.product((False, True), repeat=3):
            if not any((video, audio, subtitles)):
                continue
            with self.subTest(video=video, audio=audio, subtitles=subtitles):
                engine = self.make_engine(f"combo_{int(video)}{int(audio)}{int(subtitles)}",
                                          video=video, audio=audio, subtitles=subtitles)
                result = engine.execute()
                self.assertEqual(result["failed"], 0, self.events[-12:])
                self.assertEqual(result["processed"], 1)
                for group in ("清單一", "清單二"):
                    folder = engine.output / group
                    self.assertEqual(len(list(folder.glob("*.mp4"))), int(video))
                    self.assertEqual(len(list(folder.glob("*.mp3"))), int(audio))
                    self.assertEqual(len(list(folder.glob("*.vtt"))), int(subtitles))
                    self.assertEqual(len(list(folder.glob("*.txt"))), int(subtitles))
                    self.assertTrue(all(path.name.startswith("[會員] ") for path in folder.iterdir()))
                    if subtitles:
                        text = next(folder.glob("*.txt")).read_text(encoding="utf-8-sig")
                        self.assertEqual(text, "測試字幕 & 中文\n")
                self.assertFalse(list(engine.cache.glob("*.mp4")), "不要額外保留一份大型影片快取")
        self.assertTrue(any(kind == "progress" for kind, _ in self.events))
        self.assertTrue(any(kind == "current" for kind, _ in self.events))

    def test_reuse_video_and_extract_audio_formats(self):
        initial = self.make_engine("reuse", video=True, subtitles=False)
        self.assertEqual(initial.execute()["failed"], 0)
        self.media.unlink()  # 第二次沒有來源可下載，必須沿用已完成的影片。
        for audio_format in ("mp3", "m4a", "wav"):
            engine = self.make_engine("reuse", video=True, audio=True, subtitles=False, audio_format=audio_format)
            self.assertEqual(engine.execute()["failed"], 0, self.events[-12:])
            self.assertEqual(len(list((engine.output / "清單一").glob("*." + audio_format))), 1)

    def test_flat_output_no_member_prefix_and_chinese_languages(self):
        engine = self.make_engine("flat", categorize=False, mark_members=False,
                                  subtitle_language="所有中文字幕（繁／簡）")
        self.assertEqual(engine.execute()["failed"], 0, self.events[-12:])
        self.assertEqual(len(list(engine.output.glob("*.vtt"))), 2)
        self.assertEqual(len(list(engine.output.glob("*.txt"))), 2)
        self.assertFalse(any(path.name.startswith("[會員]") for path in engine.output.glob("*.txt")))
        self.assertFalse((engine.output / "清單一").exists())

    def test_no_matching_subtitles_is_not_a_media_failure(self):
        self.fixture_info["subtitles"] = {"zh": self.fixture_info["subtitles"]["zh"]}
        self.fixture.write_text(json.dumps(self.fixture_info, ensure_ascii=False), encoding="utf-8")
        engine = self.make_engine("missing", video=True)
        result = engine.execute()
        self.assertEqual(result["failed"], 0, self.events[-12:])
        self.assertEqual(result["no_subtitles"], 1)
        self.assertTrue(list((engine.output / "清單一").glob("*.mp4")))
        self.assertTrue(any("可用語言：zh" in str(value) for kind, value in self.events if kind == "log"))

    def test_channel_classification_and_member_hints(self):
        options = backend.DownloadOptions(url="https://www.youtube.com/@example/videos", output=self.root / "catalog", browser="")
        engine = backend.DownloadEngine(options, lambda *event: None)
        roots = [
            {"channel_id": "UC_EXAMPLE", "original_url": "https://www.youtube.com/@example/videos", "entries": [
                {"id": "public00001", "availability": "public"}, {"id": "member00001", "availability": "subscriber_only"}]},
            {"channel_id": "UC_EXAMPLE", "original_url": "https://www.youtube.com/@example/playlists", "entries": [
                {"id": "PLA", "title": "分類/同名"}, {"id": "PLB", "title": "分類:同名"}]},
        ]
        details = [
            {"id": "PLA", "entries": [{"id": "member00001", "channel_id": "UC_EXAMPLE"}]},
            {"id": "PLB", "entries": [{"id": "member00001", "channel_id": "UC_EXAMPLE"},
                                         {"id": "foreign0001", "channel_id": "UC_OTHER"}]},
            {"id": "UUMO_EXAMPLE", "entries": [{"id": "member00001", "availability": "subscriber_only"}]},
        ]
        with patch.object(engine, "fetch_lists", side_effect=[(roots, 0), (details, 0)]):
            catalog, incomplete = engine.discover()
        self.assertFalse(incomplete)
        self.assertEqual(set(catalog), {"public00001", "member00001"})
        self.assertEqual(len(catalog["member00001"].groups), 2)
        self.assertTrue(catalog["member00001"].member)
        self.assertFalse(catalog["public00001"].groups)

    def test_cancel_managed_process(self):
        engine = self.make_engine("cancel")
        ready = threading.Event()
        outcome = []
        engine.emit = lambda kind, value: ready.set() if kind == "log" and value == "ready" else None

        def work():
            try:
                engine.run_command([sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"])
            except backend.Cancelled:
                outcome.append("cancelled")

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(ready.wait(10))
        engine.cancel()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["cancelled"])
        self.assertFalse(engine.processes)

    def test_cancel_copy_preserves_complete_destination(self):
        engine = self.make_engine("atomic")
        engine.output.mkdir()
        source = engine.output / "source.mp4"
        target = engine.output / "target.mp4"
        source.write_bytes(b"x" * (8 * 1024 * 1024))
        target.write_bytes(b"original complete file")
        with patch.object(engine, "check_cancelled", side_effect=[None, backend.Cancelled()]):
            with self.assertRaises(backend.Cancelled):
                engine.copy_media(source, target)
        self.assertEqual(target.read_bytes(), b"original complete file")
        engine.copy_media(source, target)
        self.assertEqual(target.stat().st_size, source.stat().st_size)

    def test_incomplete_audio_conversion_is_replaced(self):
        engine = self.make_engine("partial_audio", audio=True, subtitles=False)
        engine.cache.mkdir(parents=True)
        (engine.cache / f"{self.video_id}.converted.mp3").write_bytes(b"incomplete")
        result = engine.execute()
        self.assertEqual(result["failed"], 0, self.events[-12:])
        target = next((engine.output / "清單一").glob("*.mp3"))
        self.assertGreater(target.stat().st_size, 1000)
        self.assertFalse(list(engine.cache.glob("*.unfinished")))

    def test_failed_audio_conversion_does_not_publish_wrong_format(self):
        silent = self.root / "silent.mp4"
        result = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(self.media),
                                 "-an", "-c:v", "copy", "-y", str(silent)], capture_output=True,
                                timeout=15, **backend.windows_process_kwargs())
        self.assertEqual(result.returncode, 0)
        self.fixture_info["formats"][0]["url"] = silent.as_uri()
        self.fixture.write_text(json.dumps(self.fixture_info, ensure_ascii=False), encoding="utf-8")
        engine = self.make_engine("audio_error", audio=True, subtitles=False)
        result = engine.execute()
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["incomplete"])
        self.assertFalse(list(engine.output.glob("*/*.mp3")))

    def test_gui_worker_download_and_progress_events(self):
        import tkinter as tk
        import downloader_ui
        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(downloader_ui, "app_directory", return_value=self.root):
                app = downloader_ui.DownloaderApp(root)
            app.settings_path = self.root / "preferences.json"
            app.url.set(self.fixture_info["webpage_url"])
            app.output.set(str(self.root / "gui_job"))
            app.video.set(True)
            app.audio.set(True)
            app.video_bitrate.set("2 Mbps")
            app.audio_bitrate.set("128 kbps")
            app.subtitles.set(True)
            app.browser.set("不使用登入")
            engine = self.make_engine("gui_job", video=True, audio=True)

            def factory(options, emit):
                engine.options = options
                engine.emit = emit
                return engine

            with patch.object(downloader_ui, "DownloadEngine", side_effect=factory):
                app.start()
            updates = 0
            deadline = time.monotonic() + 25
            while app.running and time.monotonic() < deadline:
                root.update()
                updates += 1
                time.sleep(0.02)
            self.assertFalse(app.running)
            self.assertGreater(updates, 2)
            self.assertEqual(app.phase.get(), "下載完成", app.log_text.get("1.0", "end"))
            self.assertEqual(app.overall_text.get(), "1 / 1")
            self.assertNotIn("disabled", app.start_button.state())
            self.assertIn("disabled", app.stop_button.state())
            self.assertTrue(list((engine.output / "清單一").glob("*.mp4")))
            self.assertTrue(list((engine.output / "清單一").glob("*.mp3")))
        finally:
            for event in root.tk.call("after", "info"):
                root.after_cancel(event)
            root.destroy()

    def test_gui_defaults_and_toggle_states(self):
        import tkinter as tk
        import downloader_ui
        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(downloader_ui, "app_directory", return_value=self.root):
                app = downloader_ui.DownloaderApp(root)
            self.assertTrue(app.categorize.get())
            self.assertTrue(app.mark_members.get())
            self.assertTrue(app.subtitles.get())
            self.assertIn("disabled", app.height_combo.state())
            self.assertIn("disabled", app.video_bitrate_combo.state())
            self.assertIn("disabled", app.audio_bitrate_combo.state())
            app.video.set(True)
            app.audio.set(True)
            app.subtitles.set(False)
            app._sync_option_states()
            self.assertNotIn("disabled", app.height_combo.state())
            self.assertNotIn("disabled", app.audio_combo.state())
            self.assertNotIn("disabled", app.video_bitrate_combo.state())
            self.assertNotIn("disabled", app.audio_bitrate_combo.state())
            self.assertIn("disabled", app.language_combo.state())
            app.snapshot().validate()
        finally:
            for event in root.tk.call("after", "info"):
                root.after_cancel(event)
            root.destroy()

    def test_gui_bitrate_preferences_and_wav_state(self):
        import tkinter as tk
        import downloader_ui
        for reopen in (False, True):
            root = tk.Tk()
            root.withdraw()
            try:
                with patch.object(downloader_ui, "app_directory", return_value=self.root):
                    app = downloader_ui.DownloaderApp(root)
                if not reopen:
                    app.video.set(True)
                    app.audio.set(True)
                    app.video_bitrate.set("最省空間（480p／500 kbps）")
                    app.audio_bitrate.set("128 kbps")
                    app.audio_format.set("M4A")
                    app._save_settings()
                app._sync_option_states()
                options = app.snapshot()
                self.assertEqual(options.video_bitrate, -1)
                self.assertEqual(options.audio_bitrate, 128)
                self.assertNotIn("disabled", app.audio_bitrate_combo.state())
                app.audio_format.set("WAV")
                app._sync_option_states()
                self.assertIn("disabled", app.audio_bitrate_combo.state())
                self.assertIn("未壓縮", app.audio_quality_hint.get())
                self.assertEqual(app.snapshot().media_profile("wav"), "")
                app.audio_format.set("MP3")
                app._sync_option_states()
                self.assertNotIn("disabled", app.audio_bitrate_combo.state())
                self.assertEqual(app.audio_bitrate.get(), "128 kbps")
            finally:
                for event in root.tk.call("after", "info"):
                    root.after_cancel(event)
                root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
