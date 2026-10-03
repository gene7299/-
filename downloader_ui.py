"""Windows yt-dlp 圖形介面；可直接執行或打包為獨立 EXE。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import webbrowser

from download_engine import (AUDIO_BITRATE_CHOICES, BROWSERS, LANGUAGE_CHOICES,
                             VIDEO_BITRATE_CHOICES, Cancelled, DownloadEngine,
                             DownloadOptions, app_directory, find_tool)
from dependency_manager import DependencyManager, NODE_DOWNLOAD_URL, TOOL_LABELS


APP_TITLE = "影音下載工具"
HEIGHT_CHOICES = {"最佳畫質": 0, "最高 2160p": 2160, "最高 1080p": 1080,
                  "最高 720p": 720, "最高 480p": 480}


class DownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1040x980")
        self.root.minsize(1000, 950)
        self.root.configure(bg="#f3f6fb")
        self.settings_path = app_directory() / "_downloader_settings.json"
        self.events: queue.Queue = queue.Queue()
        self.engine: DownloadEngine | None = None
        self.running = False
        self.dependencies_running = False
        self.dependency_manager: DependencyManager | None = None
        self.dependency_dialog = None
        self.close_after_dependency_stop = False
        self.close_dependency_dialog_after_stop = False
        self.close_after_stop = False
        self.editable_widgets: list = []
        self._create_variables()
        self._load_settings()
        self._create_styles()
        self._create_widgets()
        self._sync_option_states()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll_events)

    def _create_variables(self) -> None:
        self.url = tk.StringVar(value="https://www.youtube.com/@lqmama550/videos")
        self.output = tk.StringVar(value=str(app_directory() / "下載"))
        self.video = tk.BooleanVar(value=False)
        self.audio = tk.BooleanVar(value=False)
        self.subtitles = tk.BooleanVar(value=True)
        self.categorize = tk.BooleanVar(value=True)
        self.mark_members = tk.BooleanVar(value=True)
        self.subtitle_language = tk.StringVar(value="繁體中文")
        self.auto_subtitles = tk.BooleanVar(value=True)
        self.subtitle_txt = tk.BooleanVar(value=True)
        self.browser = tk.StringVar(value="Opera")
        self.cookies_file = tk.StringVar()
        self.audio_format = tk.StringVar(value="MP3")
        self.video_height = tk.StringVar(value="最佳畫質")
        self.video_bitrate = tk.StringVar(value="最高品質（保留來源）")
        self.audio_bitrate = tk.StringVar(value="最高品質")
        self.video_quality_hint = tk.StringVar()
        self.audio_quality_hint = tk.StringVar()
        self.reuse_media = tk.BooleanVar(value=True)
        self.phase = tk.StringVar(value="準備就緒")
        self.current_title = tk.StringVar(value="選擇下載項目後按「開始下載」。")
        self.overall_text = tk.StringVar(value="0 / 0")
        self.progress_text = tk.StringVar(value="等待下載")
        self.tool_status = tk.StringVar()
        self.dependency_message = tk.StringVar(value="正在檢查套件…")
        self.dependency_progress_text = tk.StringVar()
        self.setting_names = ("url", "output", "video", "audio", "subtitles", "categorize", "mark_members",
                              "subtitle_language", "auto_subtitles", "subtitle_txt", "browser", "cookies_file",
                              "audio_format", "video_height", "video_bitrate", "audio_bitrate", "reuse_media")

    def _load_settings(self) -> None:
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                return
            for name in self.setting_names:
                variable = getattr(self, name)
                value = settings.get(name)
                if (isinstance(variable, tk.BooleanVar) and isinstance(value, bool)) or (
                    isinstance(variable, tk.StringVar) and isinstance(value, str)):
                    variable.set(value)
        except (OSError, ValueError):
            pass
        for variable, choices, default in (
            (self.browser, BROWSERS, "Opera"), (self.subtitle_language, LANGUAGE_CHOICES, "繁體中文"),
            (self.video_height, HEIGHT_CHOICES, "最佳畫質"), (self.audio_format, ("MP3", "M4A", "WAV"), "MP3"),
            (self.video_bitrate, VIDEO_BITRATE_CHOICES, "最高品質（保留來源）"),
            (self.audio_bitrate, AUDIO_BITRATE_CHOICES, "最高品質"),
        ):
            if variable.get() not in choices:
                variable.set(default)

    def _save_settings(self) -> None:
        try:
            self.settings_path.write_text(json.dumps(
                {name: getattr(self, name).get() for name in self.setting_names}, ensure_ascii=False, indent=2,
            ), encoding="utf-8")
        except OSError:
            self._append_log("目前位置無法儲存偏好設定，下次開啟會使用預設值。")

    def _create_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fb")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f6fb", foreground="#1e293b", font=("Microsoft JhengHei UI", 10))
        style.configure("Card.TLabel", background="#ffffff")
        style.configure("Hint.TLabel", background="#ffffff", foreground="#64748b", font=("Microsoft JhengHei UI", 9))
        style.configure("Title.TLabel", font=("Microsoft JhengHei UI", 23, "bold"), foreground="#142b52")
        style.configure("Subtitle.TLabel", foreground="#64748b", font=("Microsoft JhengHei UI", 10))
        style.configure("TLabelframe", background="#ffffff", bordercolor="#dbe3ef", relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background="#f3f6fb", foreground="#334155",
                        font=("Microsoft JhengHei UI", 10, "bold"))
        style.configure("TCheckbutton", background="#ffffff", foreground="#1e293b", padding=(0, 1),
                        font=("Microsoft JhengHei UI", 10))
        style.map("TCheckbutton", background=[("active", "#ffffff")])
        style.configure("TButton", padding=(10, 3), font=("Microsoft JhengHei UI", 10))
        style.configure("Accent.TButton", foreground="#ffffff", background="#2563eb", padding=(22, 9),
                        font=("Microsoft JhengHei UI", 11, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#94a3b8"), ("active", "#1d4ed8")])
        style.configure("Horizontal.TProgressbar", background="#2563eb", troughcolor="#e2e8f0", borderwidth=0)

    def _track(self, widget):
        self.editable_widgets.append(widget)
        return widget

    def _entry(self, parent, variable, **kwargs):
        return self._track(ttk.Entry(parent, textvariable=variable, font=("Microsoft JhengHei UI", 10), **kwargs))

    def _check(self, parent, text, variable, **kwargs):
        return self._track(ttk.Checkbutton(parent, text=text, variable=variable,
                                          command=self._sync_option_states, **kwargs))

    def _combo(self, parent, variable, values, **kwargs):
        widget = self._track(ttk.Combobox(parent, textvariable=variable, values=list(values), state="readonly",
                                         font=("Microsoft JhengHei UI", 10), **kwargs))
        widget.bind("<<ComboboxSelected>>", lambda event: self._sync_option_states())
        return widget

    def _create_widgets(self) -> None:
        body = ttk.Frame(self.root, padding=(22, 16, 22, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1, minsize=80)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self._refresh_tool_status()
        ttk.Label(header, textvariable=self.tool_status, style="Subtitle.TLabel").grid(row=0, column=1, sticky="e")
        self.package_button = ttk.Button(header, text="檢查套件", command=self.check_dependencies)
        self.package_button.grid(row=0, column=2, sticky="e", padx=(14, 0))
        ttk.Label(header, text="影片、播放清單或頻道網址，一次選擇需要的檔案。",
                  style="Subtitle.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        source = ttk.LabelFrame(body, text="  來源與位置  ", padding=(14, 10))
        source.grid(row=1, column=0, sticky="ew", pady=(0, 11))
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="網址", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.url_entry = self._entry(source, self.url)
        self.url_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self._track(ttk.Button(source, text="貼上", command=self.paste_url)).grid(row=0, column=2, padx=(9, 0))
        ttk.Label(source, text="輸出資料夾", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 14))
        self._entry(source, self.output).grid(row=1, column=1, sticky="ew", pady=4)
        self._track(ttk.Button(source, text="選擇資料夾", command=self.choose_output)).grid(row=1, column=2, padx=(9, 0))
        ttk.Label(source, text="登入來源", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 14))
        auth = ttk.Frame(source, style="Card.TFrame")
        auth.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        auth.columnconfigure(1, weight=1)
        self._combo(auth, self.browser, BROWSERS, width=15).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.cookie_entry = self._entry(auth, self.cookies_file)
        self.cookie_entry.grid(row=0, column=1, sticky="ew")
        self.cookie_button = self._track(ttk.Button(auth, text="選擇 cookies.txt", command=self.choose_cookies))
        self.cookie_button.grid(row=0, column=2, padx=(9, 0))
        self.auth_hint = ttk.Label(auth, text="使用所選瀏覽器的登入狀態。", style="Hint.TLabel")
        self.auth_hint.grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Label(source, text="會員影片請選擇已登入且有觀看權限的瀏覽器；公開影片可選「不使用登入」。",
                  style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))

        choices = ttk.LabelFrame(body, text="  要下載的檔案  ", padding=(14, 9))
        choices.grid(row=2, column=0, sticky="ew", pady=(0, 11))
        for column in range(3):
            choices.columnconfigure(column, weight=1, uniform="options")
        video_box = ttk.Frame(choices, style="Card.TFrame")
        video_box.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._check(video_box, "影片（MP4，含原本聲音）", self.video).pack(anchor="w")
        ttk.Label(video_box, text="解析度 / 影片位元率", style="Hint.TLabel").pack(anchor="w", pady=(4, 0))
        self.height_combo = self._combo(video_box, self.video_height, HEIGHT_CHOICES, width=27)
        self.height_combo.pack(anchor="w", pady=(3, 0))
        self.video_bitrate_combo = self._combo(video_box, self.video_bitrate, VIDEO_BITRATE_CHOICES, width=27)
        self.video_bitrate_combo.pack(anchor="w", pady=(5, 0))
        ttk.Label(video_box, textvariable=self.video_quality_hint, wraplength=285,
                  style="Hint.TLabel").pack(anchor="w", pady=(7, 0))
        audio_box = ttk.Frame(choices, style="Card.TFrame")
        audio_box.grid(row=0, column=1, sticky="nsew", padx=(0, 12))
        self._check(audio_box, "音訊（另外儲存）", self.audio).pack(anchor="w")
        ttk.Label(audio_box, text="格式 / 音訊位元率", style="Hint.TLabel").pack(anchor="w", pady=(4, 0))
        self.audio_combo = self._combo(audio_box, self.audio_format, ("MP3", "M4A", "WAV"), width=27)
        self.audio_combo.pack(anchor="w", pady=(3, 0))
        self.audio_bitrate_combo = self._combo(audio_box, self.audio_bitrate, AUDIO_BITRATE_CHOICES, width=27)
        self.audio_bitrate_combo.pack(anchor="w", pady=(5, 0))
        ttk.Label(audio_box, textvariable=self.audio_quality_hint, wraplength=285,
                  style="Hint.TLabel").pack(anchor="w", pady=(7, 0))
        sub_box = ttk.Frame(choices, style="Card.TFrame")
        sub_box.grid(row=0, column=2, sticky="nsew")
        self._check(sub_box, "字幕", self.subtitles).pack(anchor="w")
        self.language_combo = self._combo(sub_box, self.subtitle_language, LANGUAGE_CHOICES, width=26)
        self.language_combo.pack(anchor="w", pady=(5, 0))
        self.auto_check = self._check(sub_box, "包含網站自動字幕", self.auto_subtitles)
        self.auto_check.pack(anchor="w", pady=(5, 0))
        self.txt_check = self._check(sub_box, "字幕另外轉存 TXT", self.subtitle_txt)
        self.txt_check.pack(anchor="w")

        organize = ttk.LabelFrame(body, text="  檔案整理  ", padding=(14, 8))
        organize.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        organize.columnconfigure(0, weight=1)
        organize.columnconfigure(1, weight=1)
        self._check(organize, "依播放清單分類（沒有清單放「未分類」）", self.categorize).grid(row=0, column=0, sticky="w")
        self._check(organize, "會員影片檔名前加上 [會員]", self.mark_members).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self._check(organize, "沿用已下載的影音檔案", self.reuse_media).grid(row=1, column=0, sticky="w")
        ttk.Label(organize, text="網站沒有字幕時，仍可下載影片或音訊。", style="Hint.TLabel").grid(row=1, column=1, sticky="w", padx=(16, 0))

        progress = ttk.Frame(body)
        progress.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        progress.columnconfigure(1, weight=1)
        ttk.Label(progress, textvariable=self.phase, font=("Microsoft JhengHei UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(progress, textvariable=self.overall_text).grid(row=0, column=2, sticky="e")
        self.overall_bar = ttk.Progressbar(progress, maximum=100)
        self.overall_bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 5))
        ttk.Label(progress, textvariable=self.current_title, style="Subtitle.TLabel", wraplength=925).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(progress, text="目前檔案", style="Subtitle.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(5, 0))
        self.file_bar = ttk.Progressbar(progress, maximum=100)
        self.file_bar.grid(row=3, column=1, sticky="ew", pady=(5, 0))
        ttk.Label(progress, textvariable=self.progress_text, style="Subtitle.TLabel", width=32, anchor="e").grid(row=3, column=2, sticky="e", padx=(10, 0), pady=(5, 0))

        logs = ttk.LabelFrame(body, text="  下載記錄  ", padding=(8, 7))
        logs.grid(row=5, column=0, sticky="nsew", pady=(0, 12))
        self.log_text = scrolledtext.ScrolledText(logs, height=7, wrap="word", font=("Microsoft JhengHei UI", 9),
                                                background="#ffffff", foreground="#334155", relief="flat", borderwidth=0)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, sticky="ew")
        actions.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(actions, text="開始下載", style="Accent.TButton", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Button(actions, text="開啟輸出資料夾", command=self.open_output).grid(row=0, column=3, sticky="e")
        self._append_log("準備就緒。字幕語言可選繁中、所有中文、全部語言或英文。")
        self._append_log("只下載網站已有的字幕；TXT 會移除時間碼與字幕標籤。")

    def _sync_option_states(self) -> None:
        if not hasattr(self, "height_combo") or self.running:
            return
        for widget, enabled in (
            (self.height_combo, self.video.get()), (self.audio_combo, self.audio.get()),
            (self.video_bitrate_combo, self.video.get()),
            (self.audio_bitrate_combo, self.audio.get() and self.audio_format.get() != "WAV"),
            (self.language_combo, self.subtitles.get()), (self.auto_check, self.subtitles.get()),
            (self.txt_check, self.subtitles.get()), (self.cookie_entry, self.browser.get() == "cookies.txt"),
            (self.cookie_button, self.browser.get() == "cookies.txt"),
        ):
            widget.state(["!disabled" if enabled else "disabled"])
        video_bitrate = VIDEO_BITRATE_CHOICES.get(self.video_bitrate.get(), 0)
        if video_bitrate == -1:
            self.video_quality_hint.set("最高 480p、影片 500 kbps、內含音訊 64 kbps。")
        elif video_bitrate:
            self.video_quality_hint.set("以所選位元率壓縮；內含音訊 128 kbps。轉檔需要一些時間。")
        else:
            self.video_quality_hint.set("保留所選解析度的來源影片，不額外壓縮。")
        if self.audio_format.get() == "WAV":
            self.audio_quality_hint.set("WAV 未壓縮，檔案較大；不套用位元率選項。")
        elif AUDIO_BITRATE_CHOICES.get(self.audio_bitrate.get(), 0):
            self.audio_quality_hint.set("使用所選位元率另存音訊；數字越低，檔案通常越小。")
        else:
            self.audio_quality_hint.set("格式相容時保留原音訊；其他情況採高品質轉換。")
        if self.browser.get() == "cookies.txt":
            self.auth_hint.grid_remove()
            self.cookie_entry.grid()
            self.cookie_button.grid()
        else:
            self.cookie_entry.grid_remove()
            self.cookie_button.grid_remove()
            self.auth_hint.grid()
            self.auth_hint.configure(text="公開影片可直接下載。" if self.browser.get() == "不使用登入"
                                     else "使用所選瀏覽器的登入狀態。")

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2500:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_tool_status(self) -> None:
        self.tool_status.set("   ·   ".join((
            "yt-dlp " + ("✓" if find_tool("yt-dlp") else "未找到"),
            "FFmpeg " + ("✓" if find_tool("ffmpeg") and find_tool("ffprobe") else "未完整安裝"),
            "Node.js " + ("✓" if find_tool("node") else "未找到"),
        )))

    def _package_check_state(self, enabled: bool) -> None:
        self.package_button.state(["!disabled" if enabled else "disabled"])
        if self.dependency_dialog is not None and self.dependency_dialog.winfo_exists():
            self.dependency_check_button.state(["!disabled" if enabled else "disabled"])

    def _create_dependency_dialog(self) -> None:
        if self.dependency_dialog is not None and self.dependency_dialog.winfo_exists():
            self.dependency_dialog.lift()
            return
        dialog = self.dependency_dialog = tk.Toplevel(self.root)
        dialog.title("檢查套件")
        dialog.geometry("820x480")
        dialog.minsize(760, 460)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self.close_dependency_dialog)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="檢查版本與更新套件", font=("Microsoft JhengHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(body, text="缺少本地版本或有新版的 yt-dlp、FFmpeg／ffprobe 會自動下載至：\n" + str(app_directory() / "bin"),
                  wraplength=740, style="Subtitle.TLabel").pack(anchor="w", pady=(7, 14))
        self.dependency_table = ttk.Treeview(body, columns=("current", "latest", "state"), height=4)
        self.dependency_table.heading("#0", text="套件")
        self.dependency_table.heading("current", text="目前版本")
        self.dependency_table.heading("latest", text="最新版本 / 需求")
        self.dependency_table.heading("state", text="狀態")
        self.dependency_table.column("#0", width=110, minwidth=95, stretch=False)
        self.dependency_table.column("current", width=145, minwidth=115)
        self.dependency_table.column("latest", width=155, minwidth=135)
        self.dependency_table.column("state", width=260, minwidth=195)
        for name, label in TOOL_LABELS.items():
            self.dependency_table.insert("", "end", iid=name, text=label, values=("—", "—", "等待檢查"))
        self.dependency_table.pack(fill="both", expand=True)
        ttk.Label(body, textvariable=self.dependency_message, wraplength=740).pack(anchor="w", pady=(12, 5))
        self.dependency_bar = ttk.Progressbar(body, maximum=100)
        self.dependency_bar.pack(fill="x")
        ttk.Label(body, textvariable=self.dependency_progress_text, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self.dependency_check_button = ttk.Button(buttons, text="再次檢查", command=self.check_dependencies)
        self.dependency_check_button.pack(side="left")
        self.dependency_cancel_button = ttk.Button(buttons, text="停止", command=self.cancel_dependencies)
        self.dependency_cancel_button.pack(side="left", padx=8)
        self.node_install_button = ttk.Button(buttons, text="安裝 Node.js", command=self.open_node_download, state="disabled")
        self.node_install_button.pack(side="left")
        ttk.Button(buttons, text="關閉", command=self.close_dependency_dialog).pack(side="right")

    def check_dependencies(self) -> None:
        if self.running or self.dependencies_running:
            return
        try:
            manager = DependencyManager(lambda kind, value: self.events.put(("dependency_" + kind, value)),
                                        root=app_directory())
        except OSError as error:
            messagebox.showerror("無法檢查套件", str(error), parent=self.root)
            return
        self._create_dependency_dialog()
        self.dependencies_running = True
        self.dependency_manager = manager
        self.close_dependency_dialog_after_stop = False
        self.close_after_dependency_stop = False
        self.dependency_message.set("正在檢查本機版本與最新發行版本…")
        self.dependency_progress_text.set("詳細路徑與錯誤會顯示在主視窗的下載記錄。")
        self.dependency_bar["value"] = 0
        for name in TOOL_LABELS:
            self.dependency_table.item(name, values=("—", "—", "等待檢查"))
        self._package_check_state(False)
        self.start_button.state(["disabled"])
        self.dependency_cancel_button.state(["!disabled"])
        self.node_install_button.state(["disabled"])
        threading.Thread(target=self._dependency_worker, args=(manager,), daemon=True).start()

    def _dependency_worker(self, manager: DependencyManager) -> None:
        try:
            result = manager.check_and_update()
        except Cancelled:
            result = {"cancelled": True, "errors": [], "node_required": False}
        except Exception as error:
            result = {"errors": [str(error)], "node_required": False}
        self.events.put(("dependency_finished", result))

    def cancel_dependencies(self) -> None:
        if self.dependencies_running and self.dependency_manager is not None:
            self.dependency_message.set("正在停止套件檢查…")
            self.dependency_cancel_button.state(["disabled"])
            threading.Thread(target=self.dependency_manager.cancel, daemon=True).start()

    def close_dependency_dialog(self) -> None:
        if self.dependencies_running:
            self.close_dependency_dialog_after_stop = True
            self.cancel_dependencies()
        elif self.dependency_dialog is not None and self.dependency_dialog.winfo_exists():
            self.dependency_dialog.destroy()
            self.dependency_dialog = None

    def open_node_download(self) -> None:
        if not webbrowser.open(NODE_DOWNLOAD_URL):
            self._append_log("請開啟 Node.js 官方安裝頁：" + NODE_DOWNLOAD_URL)

    def _finish_dependencies(self, result: dict) -> None:
        self.dependencies_running = False
        self.dependency_manager = None
        self._package_check_state(not self.running)
        if not self.running:
            self.start_button.state(["!disabled"])
        self._refresh_tool_status()
        self.dependency_cancel_button.state(["disabled"])
        if result.get("cancelled"):
            self.dependency_message.set("已停止；原有套件與已完成的更新會保留。")
        elif result.get("errors"):
            self.dependency_message.set("部分套件檢查或更新失敗；詳細原因請查看主視窗記錄。")
            for error in result["errors"]:
                self._append_log("套件檢查：" + error)
        else:
            self.dependency_message.set("套件檢查完成，可使用目前已安裝的版本。")
        if result.get("node_required"):
            self.node_install_button.state(["!disabled"])
            self.dependency_message.set("請安裝 Node.js 22 以上版本；安裝完成後按「再次檢查」。")
            if not self.close_after_dependency_stop and not self.close_dependency_dialog_after_stop:
                self.open_node_download()
        self._append_log("套件檢查已停止。" if result.get("cancelled") else "套件檢查完成。")
        if self.close_after_dependency_stop:
            self.root.after_idle(self.root.destroy)
        elif self.close_dependency_dialog_after_stop:
            self.close_dependency_dialog()

    def paste_url(self) -> None:
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="選擇輸出資料夾", initialdir=self.output.get())
        if selected:
            self.output.set(selected)

    def choose_cookies(self) -> None:
        selected = filedialog.askopenfilename(parent=self.root, title="選擇 cookies.txt",
                                              filetypes=[("Cookie 檔案", "*.txt"), ("所有檔案", "*.*")])
        if selected:
            self.cookies_file.set(selected)

    def open_output(self) -> None:
        path = Path(self.output.get().strip()).expanduser()
        if not path.is_dir():
            messagebox.showinfo(APP_TITLE, "輸出資料夾尚未建立，開始下載後會自動建立。", parent=self.root)
            return
        if os.name == "nt":
            os.startfile(str(path.resolve()))

    def snapshot(self) -> DownloadOptions:
        if not self.output.get().strip():
            raise ValueError("請選擇輸出資料夾。")
        return DownloadOptions(
            url=self.url.get().strip(), output=Path(self.output.get().strip()),
            video=self.video.get(), audio=self.audio.get(), subtitles=self.subtitles.get(),
            categorize=self.categorize.get(), mark_members=self.mark_members.get(),
            subtitle_language=self.subtitle_language.get(), auto_subtitles=self.auto_subtitles.get(),
            subtitle_txt=self.subtitle_txt.get(), browser=BROWSERS[self.browser.get()],
            cookies_file=self.cookies_file.get().strip(), audio_format=self.audio_format.get().lower(),
            max_height=HEIGHT_CHOICES[self.video_height.get()], reuse_media=self.reuse_media.get(),
            video_bitrate=VIDEO_BITRATE_CHOICES[self.video_bitrate.get()],
            audio_bitrate=AUDIO_BITRATE_CHOICES[self.audio_bitrate.get()],
        )

    def start(self) -> None:
        if self.running or self.dependencies_running:
            return
        try:
            options = self.snapshot()
            options.validate()
        except (ValueError, KeyError) as error:
            messagebox.showerror("請檢查設定", str(error), parent=self.root)
            return
        self._save_settings()
        self.running = True
        self.close_after_stop = False
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self._package_check_state(False)
        for widget in self.editable_widgets:
            widget.state(["disabled"])
        self.overall_bar["value"] = self.file_bar["value"] = 0
        self.overall_text.set("讀取中")
        self.progress_text.set("等待下載")
        self.phase.set("正在開始…")
        self._append_log("\n──────── 開始下載 ────────")
        self.engine = DownloadEngine(options, lambda kind, value: self.events.put((kind, value)))
        threading.Thread(target=self._worker, args=(self.engine,), daemon=True).start()

    def _worker(self, engine: DownloadEngine) -> None:
        try:
            result = engine.execute()
            result["state"] = "partial" if result["failed"] or result["incomplete"] else "completed"
            self.events.put(("finished", result))
        except Cancelled:
            self.events.put(("finished", {"state": "cancelled", "processed": engine.processed}))
        except Exception as error:
            self.events.put(("log", "錯誤：" + str(error)))
            self.events.put(("finished", {"state": "error"}))

    def stop(self) -> None:
        if self.running and self.engine is not None:
            self.stop_button.state(["disabled"])
            self.phase.set("正在停止…")
            self.engine.cancelled.set()
            threading.Thread(target=self.engine.cancel, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            for _ in range(200):
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(value))
                elif kind == "phase":
                    self.phase.set(value)
                elif kind == "current":
                    title = str(value)
                    self.current_title.set(title if len(title) <= 60 else title[:60] + "…")
                    self._append_log("正在處理：" + title)
                    self.file_bar["value"] = 0
                    self.progress_text.set("準備下載")
                elif kind == "progress":
                    total = value.get("total_bytes") or value.get("total_bytes_estimate") or 0
                    percent = min(100.0, value.get("downloaded_bytes", 0) / total * 100) if total else 0
                    self.file_bar["value"] = percent
                    speed = value.get("speed") or 0
                    eta = value.get("eta")
                    tail = f"{speed / 1048576:.1f} MB/s" if speed else ""
                    if eta is not None:
                        tail += f"  剩餘 {int(eta)} 秒"
                    self.progress_text.set(f"{percent:.0f}%  {tail}")
                elif kind == "convert_progress":
                    percent = value.get("percent", 0)
                    self.file_bar["value"] = percent
                    eta = value.get("eta")
                    tail = f"  剩餘 {int(eta)} 秒" if eta is not None else ""
                    self.progress_text.set(f"轉檔 {percent:.0f}%{tail}")
                elif kind == "count":
                    done, total = value["done"], value["total"]
                    self.overall_text.set(f"{done} / {total}")
                    self.overall_bar["value"] = done / total * 100 if total else 0
                    if value.get("title"):
                        title = value["title"]
                        self.current_title.set(title if len(title) <= 60 else title[:60] + "…")
                elif kind == "finished":
                    self._finish(value)
                elif kind == "dependency_log":
                    self._append_log("套件：" + str(value))
                elif kind == "dependency_status":
                    self.dependency_table.item(value["name"], values=(value["current"], value["latest"] or "—", value["state"]))
                elif kind == "dependency_progress":
                    downloaded, total = value["downloaded"], value["total"]
                    self.dependency_bar["value"] = min(100, downloaded / total * 100) if total else 0
                    text = f"{TOOL_LABELS[value['name']]}：{downloaded / 1048576:.1f} MB"
                    if total:
                        text += f" / {total / 1048576:.1f} MB"
                    self.dependency_progress_text.set(text)
                elif kind == "dependency_finished":
                    self._finish_dependencies(value)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _finish(self, result: dict) -> None:
        self.running = False
        self._package_check_state(True)
        self.start_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        for widget in self.editable_widgets:
            widget.state(["!disabled"])
        self._sync_option_states()
        state = result["state"]
        label = {"completed": "下載完成", "partial": "處理完成，部分項目有錯誤", "cancelled": "已停止", "error": "下載失敗，請查看記錄"}[state]
        self.phase.set(label)
        self._append_log(label)
        if state in ("completed", "partial"):
            self._append_log(f"已處理 {result['processed']} / {result['total']} 支；"
                             f"失敗 {result['failed']} 支；缺少所選字幕 {result['no_subtitles']} 支。")
            self.progress_text.set("處理結束")
        elif state == "cancelled":
            self._append_log("已完成的檔案會保留；重跑可沿用影音並繼續下載未完成的檔案。")
        self.engine = None
        if self.close_after_stop:
            self.root.after_idle(self.root.destroy)

    def close(self) -> None:
        if self.dependencies_running:
            self._save_settings()
            self.close_after_dependency_stop = True
            self.cancel_dependencies()
        elif self.running:
            if messagebox.askyesno(APP_TITLE, "下載仍在進行，是否停止並關閉？", parent=self.root):
                self._save_settings()
                self.close_after_stop = True
                self.stop()
        else:
            self._save_settings()
            self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--self-test", type=Path, metavar="RESULT_JSON", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-online", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    app = DownloaderApp(root)
    if args.self_test:
        root.withdraw()

        def verify():
            options = app.snapshot()
            options.validate()
            result = {"gui": True, "categorize": options.categorize, "mark_members": options.mark_members,
                      "yt_dlp": bool(find_tool("yt-dlp")), "ffmpeg": bool(find_tool("ffmpeg")),
                      "video_bitrate": options.video_bitrate, "audio_bitrate": options.audio_bitrate,
                      "bitrate_options_available": bool(app.video_bitrate_combo["values"] and app.audio_bitrate_combo["values"]),
                      "package_button_available": bool(app.package_button.winfo_exists()),
                      "frozen": bool(getattr(sys, "frozen", False))}
            engine = DownloadEngine(options, lambda *event: None)
            if engine.yt_dlp:
                result["external_ytdlp_status"] = engine.run_command([engine.yt_dlp, "--version"])
            if engine.ffmpeg:
                result["external_ffmpeg_status"] = engine.run_command([engine.ffmpeg, "-version"])
            manager = DependencyManager(lambda *event: None)
            result["tool_versions"] = {name: manager.inspect_local(name).version for name in TOOL_LABELS}
            if args.self_test_online:
                try:
                    result["remote_versions"] = {"yt-dlp": manager.ytdlp_release().version,
                                                 "ffmpeg": manager.ffmpeg_release().version}
                except Exception as error:
                    result["online_error"] = str(error)
            args.self_test.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            root.destroy()

        root.after(200, verify)
    root.mainloop()


if __name__ == "__main__":
    main()
