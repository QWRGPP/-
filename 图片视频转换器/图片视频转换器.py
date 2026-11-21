#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音视频 / 图片格式批量转换工具
=============================
基于 Pillow（图片）与 FFmpeg（视频）的格式互转工具，提供图形界面 (Tkinter)
与命令行两种用法，界面顶部可在「图片转换 / 视频转换」两种模式间切换。

图片支持输入（Pillow 可解码）:
    JPG / JPEG / PNG / BMP / GIF / TIFF / TIF / WebP / ICO / PPM / PGM /
    PBM / TGA / PCX / JP2 / J2K 等
图片输出:
    JPEG / PNG / BMP / GIF / TIFF / WebP / ICO / PPM / TGA

视频支持输入（FFmpeg 可解码的常见容器）:
    MP4 / MKV / AVI / MOV / WMV / FLV / WebM / TS / M4V / 3GP / MPG / MPEG /
    RMVB / RM / VOB / M2TS / MTS / F4V / OGV / DIVX / ASF 等
视频输出容器:
    MP4 / MKV / MOV / AVI / WMV / FLV / WebM / TS / M4V / 3GP / MPG

依赖:
    pip install Pillow
    视频转码需 FFmpeg。程序启动时自动按以下顺序查找 ffmpeg / ffprobe：
      1) 系统 PATH；2) 程序根目录及其 bin、ffmpeg/bin 等常见子目录（自动扫描，无需手动设置）
    也可将下载的 ffmpeg.exe / ffprobe.exe 直接放在本脚本同级目录
    （下载地址 https://ffmpeg.org/ ）；命令行可用 --ffmpeg 显式覆盖。

图形界面运行:
    python3 image_format_converter.py

命令行示例（图片）:
    python3 image_format_converter.py --input a.jpg --format png --output ./out
    python3 image_format_converter.py --input-dir ./in --format webp --output-dir ./out

命令行示例（视频）:
    python3 image_format_converter.py --mode video --input a.mkv --vformat mp4 --output ./out/a.mp4
    python3 image_format_converter.py --mode video --input-dir ./in --vformat webm --output-dir ./out
"""

import os
import sys
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Pillow 导入与基础配置
# ---------------------------------------------------------------------------
try:
    from PIL import Image
except ImportError:
    Image = None

# 输入图片常见扩展名（用于文件筛选与目录递归扫描）
INPUT_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff",
    ".webp", ".ico", ".ppm", ".pgm", ".pbm", ".tga", ".pcx",
    ".jp2", ".j2k", ".jxl", ".flif", ".xbm", ".eps",
}

# 可导出的目标图片格式: 界面标签 -> (Pillow 格式名, 文件后缀)
OUTPUT_FORMATS = {
    "JPEG (.jpg)":  ("JPEG", ".jpg"),
    "PNG (.png)":   ("PNG",  ".png"),
    "BMP (.bmp)":   ("BMP",  ".bmp"),
    "GIF (.gif)":   ("GIF",  ".gif"),
    "TIFF (.tiff)": ("TIFF", ".tiff"),
    "WebP (.webp)": ("WebP",  ".webp"),
    "ICO (.ico)":   ("ICO",  ".ico"),
    "PPM (.ppm)":   ("PPM",  ".ppm"),
    "TGA (.tga)":   ("TGA",   ".tga"),
}

# JPEG / WebP 不支持透明，转换时用此底色填充透明区域
TRANSPARENT_FILL = (255, 255, 255)


# ---------------------------------------------------------------------------
# 图片核心转换逻辑
# ---------------------------------------------------------------------------
def prepare_image(img, target_format):
    """根据目标格式调整图像模式，处理透明通道与色彩模式。"""
    mode = img.mode

    if target_format == "JPEG":
        if mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, TRANSPARENT_FILL)
            background.paste(img, mask=img.getchannel("A"))
            return background
        if mode == "P":
            rgba = img.convert("RGBA")
            background = Image.new("RGB", img.size, TRANSPARENT_FILL)
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        if mode not in ("RGB", "L"):
            return img.convert("RGB")
        return img

    if target_format == "WebP":
        if mode not in ("RGB", "RGBA", "L", "LA", "P"):
            return img.convert("RGB")
        return img

    if target_format == "BMP":
        if mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, TRANSPARENT_FILL)
            background.paste(img, mask=img.getchannel("A"))
            return background
        if mode not in ("RGB", "L", "P"):
            return img.convert("RGB")
        return img

    if target_format == "GIF":
        return img

    if mode not in ("RGB", "RGBA", "L", "LA", "P"):
        return img.convert("RGB")
    return img


def convert_one(src_path, dst_path, target_format, quality=95):
    """转换单张图片。成功返回 True，失败抛出异常。"""
    with Image.open(src_path) as img:
        img.load()
        img = prepare_image(img, target_format)

        save_kwargs = {}
        if target_format == "JPEG":
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True
            save_kwargs["progressive"] = True
        elif target_format == "WebP":
            save_kwargs["quality"] = quality
            save_kwargs["method"] = 6
        elif target_format == "TIFF":
            save_kwargs["compression"] = "tiff_lzw"

        os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
        img.save(dst_path, format=target_format, **save_kwargs)
    return True


def scan_by_ext(folder, exts, recursive=True):
    """扫描目录下指定扩展名的文件，返回路径列表。"""
    result = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if os.path.splitext(name)[1].lower() in exts:
                    result.append(os.path.join(root, name))
    else:
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in exts:
                result.append(full)
    return result


# ---------------------------------------------------------------------------
# 视频（FFmpeg）核心逻辑
# ---------------------------------------------------------------------------
# 可导出的目标视频容器: 界面标签 -> (容器后缀, 视频编码, 音频编码)
VIDEO_FORMATS = {
    "MP4 (.mp4)":    ("mp4",  "libx264",     "aac"),
    "MKV (.mkv)":    ("mkv",  "libx264",     "aac"),
    "MOV (.mov)":    ("mov",  "libx264",     "aac"),
    "AVI (.avi)":    ("avi",  "mpeg4",       "mp3"),
    "WMV (.wmv)":    ("wmv",  "wmv2",        "wmav2"),
    "FLV (.flv)":    ("flv",  "libx264",     "aac"),
    "WebM (.webm)":  ("webm", "libvpx-vp9",  "libopus"),
    "MPEG-TS (.ts)": ("ts",   "libx264",     "aac"),
    "M4V (.m4v)":    ("m4v",  "libx264",     "aac"),
    "3GP (.3gp)":    ("3gp",  "mpeg4",       "aac"),
    "MPEG (.mpg)":   ("mpg",  "mpeg2video",  "mp2"),
}

# 输入视频常见扩展名
VIDEO_INPUT_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v",
    ".3gp", ".mpg", ".mpeg", ".rmvb", ".rm", ".vob", ".m2ts", ".mts",
    ".f4v", ".ogv", ".divx", ".asf", ".mxf", ".dv", ".gif",
}

# 支持 CRF 恒定质量模式的编码器
_CRF_CODECS = {"libx264", "libx265", "libvpx-vp9", "libvpx", "libaom-av1"}


def _app_base_dir():
    """程序根目录：打包成 exe 时取可执行文件目录，脚本运行时取脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _scan_bundled_tools():
    """在程序根目录及常见子目录中自动扫描 ffmpeg / ffprobe。"""
    base = _app_base_dir()
    exe = ".exe" if os.name == "nt" else ""
    want_ff = {"ffmpeg" + exe, "ffmpeg"}
    want_fp = {"ffprobe" + exe, "ffprobe"}
    ffmpeg = ffprobe = None

    # 常见相对目录：根目录、bin、解压后的 ffmpeg 目录及其 bin
    rel_dirs = [".", "bin", "ffmpeg", "ffmpeg/bin", "ffmpeg/bin/x64",
                "ffmpeg/bin/Release", "build", "build/bin"]
    for rel in rel_dirs:
        dirp = os.path.join(base, rel)
        if not os.path.isdir(dirp):
            continue
        try:
            names = os.listdir(dirp)
        except OSError:
            continue
        for name in names:
            low = name.lower()
            full = os.path.join(dirp, name)
            if not os.path.isfile(full):
                continue
            if low in want_ff and not ffmpeg:
                ffmpeg = full
            elif low in want_fp and not ffprobe:
                ffprobe = full

    # 兜底：深度不超过 3 层递归扫描
    if not ffmpeg or not ffprobe:
        for root, dirs, files in os.walk(base):
            depth = os.path.relpath(root, base).count(os.sep)
            if depth >= 3:
                dirs[:] = []
                continue
            for name in files:
                low = name.lower()
                full = os.path.join(root, name)
                if low in want_ff and not ffmpeg:
                    ffmpeg = full
                elif low in want_fp and not ffprobe:
                    ffprobe = full

    # 只找到 ffmpeg 时，在同目录补找 ffprobe
    if ffmpeg and not ffprobe:
        cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + exe)
        if os.path.exists(cand):
            ffprobe = cand
    return ffmpeg, ffprobe


def find_ffmpeg_tools(manual_path=None):
    """定位 ffmpeg 与 ffprobe。

    查找优先级：手动指定路径 > 系统 PATH > 程序根目录（含常见 bin 子目录）捆绑文件。
    返回 (ffmpeg, ffprobe)，缺失项为 None。
    """
    exe = ".exe" if os.name == "nt" else ""
    if manual_path:
        ffmpeg = manual_path
        ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + exe)
        if not os.path.exists(ffprobe):
            ffprobe = shutil.which("ffprobe")
        return ffmpeg if os.path.exists(ffmpeg) else None, ffprobe

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        bundled_ff, bundled_fp = _scan_bundled_tools()
        ffmpeg = ffmpeg or bundled_ff
        ffprobe = ffprobe or bundled_fp
    if ffmpeg and not ffprobe:
        candidate = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + exe)
        if os.path.exists(candidate):
            ffprobe = candidate
    return ffmpeg, ffprobe


def quality_to_crf(quality):
    """界面画质 1-100 映射为 CRF 30(低质) ~ 18(高质)。"""
    quality = max(1, min(100, quality))
    return round(30 - (quality - 1) / 99 * 12)


def probe_duration(ffprobe, src):
    """用 ffprobe 获取视频时长（秒），失败返回 None。"""
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def build_transcode_cmd(ffmpeg, src, dst, vcodec, acodec, crf):
    """构造 FFmpeg 转码命令。"""
    cmd = [ffmpeg, "-y", "-i", src]
    cmd += ["-c:v", vcodec]
    if vcodec in _CRF_CODECS:
        cmd += ["-crf", str(crf), "-preset", "medium"]
    if acodec:
        cmd += ["-c:a", acodec]
    # 保留第一条视频流；音频流可选（无源音频时不报错）
    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [dst]
    return cmd


def transcode_one(src, dst, vcodec, acodec, quality, ffmpeg, ffprobe,
                  progress_cb=None, cancel_check=None):
    """转码单个视频。progress_cb(0~1)，cancel_check() 返回 True 则取消。"""
    duration = probe_duration(ffprobe, src)
    crf = quality_to_crf(quality)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    cmd = build_transcode_cmd(ffmpeg, src, dst, vcodec, acodec, crf)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )

    stderr_tail = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if cancel_check and cancel_check():
                proc.terminate()
                raise RuntimeError("已手动取消")
            if line.startswith("out_time_us=") and duration:
                try:
                    us = int(line.split("=", 1)[1])
                    if us > 0 and progress_cb:
                        progress_cb(min(1.0, us / 1_000_000 / duration))
                except ValueError:
                    pass
            elif line.startswith("out_time_ms=") and duration:
                try:
                    ms = int(line.split("=", 1)[1])
                    if ms > 0 and progress_cb:
                        progress_cb(min(1.0, ms / 1000 / duration))
                except ValueError:
                    pass

        _, stderr = proc.communicate(timeout=30)
        if stderr:
            stderr_tail = stderr.strip().splitlines()[-15:]
        if proc.returncode != 0:
            raise RuntimeError("FFmpeg 退出码 %s：%s" %
                               (proc.returncode, " | ".join(stderr_tail[-5:])))
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("转码超时")
    return True


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
class _FileListPanel(ttk.Frame):
    """文件列表面板基类，图片 / 视频面板共用。"""

    kind = "base"
    open_title = "选择文件"
    folder_title = "选择文件夹"
    unit = "个"

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.file_list = []
        self._build_list_area()

    def _build_list_area(self):
        list_frame = ttk.LabelFrame(self, text="待转换文件")
        list_frame.pack(fill="both", expand=True, padx=8, pady=6)

        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=10)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="添加文件", command=self.add_files).pack(side="left", padx=4)
        ttk.Button(btn_row, text="添加文件夹", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(btn_row, text="移除选中", command=self.remove_selected).pack(side="left", padx=4)
        ttk.Button(btn_row, text="清空列表", command=self.clear_list).pack(side="left", padx=4)
        self.count_label = ttk.Label(btn_row, text=f"共 0 {self.unit}")
        self.count_label.pack(side="right")

    # --- 子类提供 ---
    def _input_exts(self):
        return INPUT_EXTS

    def add_files(self):
        exts = self._input_exts()
        paths = filedialog.askopenfilenames(
            title=self.open_title,
            filetypes=[("媒体文件", " ".join("*" + e for e in sorted(exts))),
                       ("所有文件", "*.*")],
        )
        self._add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title=self.folder_title)
        if folder:
            self._add_paths(scan_by_ext(folder, self._input_exts(), recursive=True))

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.file_list and os.path.isfile(p):
                self.file_list.append(p)
                self.listbox.insert(tk.END, p)
                added += 1
        self.count_label.config(text=f"共 {len(self.file_list)} {self.unit}")
        self.app.log(f"添加 {added} 个，当前列表 {len(self.file_list)} 个")

    def remove_selected(self):
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)
            del self.file_list[idx]
        self.count_label.config(text=f"共 {len(self.file_list)} {self.unit}")

    def clear_list(self):
        self.file_list.clear()
        self.listbox.delete(0, tk.END)
        self.count_label.config(text=f"共 0 {self.unit}")

    # --- 子类覆写 ---
    def validate(self):
        """返回 (ok, 提示信息)。"""
        return True, ""

    def start(self):
        raise NotImplementedError


class ImagePanel(_FileListPanel):
    kind = "image"
    open_title = "选择图片"
    folder_title = "选择图片文件夹"
    unit = "张"

    def _input_exts(self):
        return INPUT_EXTS

    def _build(self):
        opt = ttk.LabelFrame(self, text="图片转换设置")
        opt.pack(fill="x", padx=8, pady=6)

        ttk.Label(opt, text="目标格式:").grid(row=0, column=0, padx=8, pady=10, sticky="w")
        self.format_var = tk.StringVar(value="PNG (.png)")
        ttk.Combobox(opt, textvariable=self.format_var, state="readonly",
                     values=list(OUTPUT_FORMATS.keys()), width=18).grid(
            row=0, column=1, padx=8, pady=10, sticky="w")

        ttk.Label(opt, text="画质 (1-100):").grid(row=0, column=2, padx=8, pady=10, sticky="w")
        self.quality_var = tk.IntVar(value=95)
        ttk.Spinbox(opt, from_=1, to=100, textvariable=self.quality_var, width=6).grid(
            row=0, column=3, padx=8, pady=10, sticky="w")

        ttk.Label(opt, text="输出目录:").grid(row=1, column=0, padx=8, pady=10, sticky="w")
        self.output_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.output_var).grid(
            row=1, column=1, columnspan=2, padx=8, pady=10, sticky="we")
        ttk.Button(opt, text="浏览…", command=self._choose_output).grid(
            row=1, column=3, padx=8, pady=10)
        opt.columnconfigure(1, weight=1)

    def _choose_output(self):
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_var.set(folder)

    def validate(self):
        if Image is None:
            return False, "未检测到 Pillow 库，请先安装：pip install Pillow"
        if not self.file_list:
            return False, "请先添加需要转换的图片。"
        return True, ""

    def start(self):
        target_label = self.format_var.get()
        target_format, target_ext = OUTPUT_FORMATS[target_label]
        quality = self.quality_var.get()
        output_dir = self.output_var.get().strip()
        self.app.run_image(list(self.file_list), target_format, target_ext, quality, output_dir)


class VideoPanel(_FileListPanel):
    kind = "video"
    open_title = "选择视频"
    folder_title = "选择视频文件夹"
    unit = "个"

    def _input_exts(self):
        return VIDEO_INPUT_EXTS

    def _build(self):
        opt = ttk.LabelFrame(self, text="视频转换设置")
        opt.pack(fill="x", padx=8, pady=6)

        ttk.Label(opt, text="目标格式:").grid(row=0, column=0, padx=8, pady=10, sticky="w")
        self.format_var = tk.StringVar(value="MP4 (.mp4)")
        ttk.Combobox(opt, textvariable=self.format_var, state="readonly",
                     values=list(VIDEO_FORMATS.keys()), width=18).grid(
            row=0, column=1, padx=8, pady=10, sticky="w")

        ttk.Label(opt, text="画质 (1-100):").grid(row=0, column=2, padx=8, pady=10, sticky="w")
        self.quality_var = tk.IntVar(value=80)
        ttk.Spinbox(opt, from_=1, to=100, textvariable=self.quality_var, width=6).grid(
            row=0, column=3, padx=8, pady=10, sticky="w")

        ttk.Label(opt, text="输出目录:").grid(row=1, column=0, padx=8, pady=10, sticky="w")
        self.output_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.output_var).grid(
            row=1, column=1, columnspan=2, padx=8, pady=10, sticky="we")
        ttk.Button(opt, text="浏览…", command=self._choose_output).grid(
            row=1, column=3, padx=8, pady=10)
        opt.columnconfigure(1, weight=1)

    def _choose_output(self):
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_var.set(folder)

    def tools(self):
        return find_ffmpeg_tools()

    def validate(self):
        if not self.file_list:
            return False, "请先添加需要转换的视频。"
        ffmpeg, _ = self.tools()
        if not ffmpeg:
            return False, ("未找到 FFmpeg。程序已自动扫描系统 PATH 及本程序目录，\n"
                           "请将 ffmpeg.exe / ffprobe.exe 放到程序目录或其 bin 子目录，\n"
                           "或安装 FFmpeg 并加入 PATH。\n"
                           "下载地址：https://ffmpeg.org/download.html")
        return True, ""

    def start(self):
        target_label = self.format_var.get()
        container, vcodec, acodec = VIDEO_FORMATS[target_label]
        target_ext = "." + container
        quality = self.quality_var.get()
        output_dir = self.output_var.get().strip()
        ffmpeg, ffprobe = self.tools()
        self.app.run_video(list(self.file_list), container, target_ext,
                           vcodec, acodec, quality, output_dir, ffmpeg, ffprobe)


class ConverterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("音视频 / 图片格式批量转换工具")
        self.geometry("800x660")
        self.minsize(740, 600)

        self.busy = False
        self.cancel_flag = False
        self._proc = None

        self._build_ui()

        if Image is None:
            self.log("提示：未检测到 Pillow，图片模式不可用。安装：pip install Pillow")
        ffmpeg, _ = find_ffmpeg_tools()
        if ffmpeg:
            self.log(f"已自动检测到 FFmpeg：{ffmpeg}")
        else:
            self.log("提示：未检测到 FFmpeg，视频模式不可用。请将 ffmpeg/ffprobe 放入程序目录或安装到系统 PATH。")

    # ------------------------- 界面搭建 -------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # 顶部模式选项栏（页签）
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, **pad)

        self.image_panel = ImagePanel(self.notebook, self)
        self.image_panel._build()
        self.video_panel = VideoPanel(self.notebook, self)
        self.video_panel._build()

        self.notebook.add(self.image_panel, text="  图片转换  ")
        self.notebook.add(self.video_panel, text="  视频转换  ")

        # 底部共享：进度 / 状态
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress.pack(fill="x", side="left", expand=True)
        self.status_label = ttk.Label(prog_frame, text="就绪", width=20)
        self.status_label.pack(side="right", padx=8)

        # 共享日志
        log_frame = ttk.LabelFrame(self, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        log_scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)

        # 操作按钮
        action_row = ttk.Frame(self)
        action_row.pack(fill="x", **pad)
        self.cancel_btn = ttk.Button(action_row, text="取消", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=4)
        self.start_btn = ttk.Button(action_row, text="开始转换", command=self._on_start)
        self.start_btn.pack(side="right", padx=4)

    def _current_panel(self):
        idx = self.notebook.index(self.notebook.select())
        return self.image_panel if idx == 0 else self.video_panel

    # ------------------------- 日志 / 进度 -------------------------
    def log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _set_progress(self, value, maximum=None):
        if maximum is not None:
            self.progress.config(maximum=maximum)
        self.progress.config(value=value)

    # ------------------------- 启动 / 取消 -------------------------
    def _on_start(self):
        if self.busy:
            return
        panel = self._current_panel()
        ok, msg = panel.validate()
        if not ok:
            messagebox.showwarning("无法开始", msg)
            return
        self.cancel_flag = False
        panel.start()

    def _cancel(self):
        self.cancel_flag = True
        self.log("正在取消当前任务…")

    def _set_busy(self, busy):
        self.busy = busy
        self.start_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    # ------------------------- 图片任务 -------------------------
    def run_image(self, files, target_format, target_ext, quality, output_dir):
        self._set_busy(True)
        self.log("=" * 50)
        self.log(f"[图片] 开始转换 → {target_format}，画质 {quality}")
        threading.Thread(
            target=self._image_worker,
            args=(files, target_format, target_ext, quality, output_dir),
            daemon=True,
        ).start()

    def _image_worker(self, files, target_format, target_ext, quality, output_dir):
        total = len(files)
        ok = fail = 0
        self.after(0, self._set_progress, 0, total)

        for i, src in enumerate(files, start=1):
            if self.cancel_flag:
                self.after(0, self.log, "[取消] 任务已中止")
                break
            try:
                if output_dir:
                    base = os.path.splitext(os.path.basename(src))[0]
                    dst = os.path.join(output_dir, base + target_ext)
                else:
                    dst = os.path.splitext(src)[0] + target_ext

                if os.path.abspath(src) == os.path.abspath(dst):
                    self.after(0, self.log, f"[跳过] 已是目标格式：{os.path.basename(src)}")
                    ok += 1
                    continue

                convert_one(src, dst, target_format, quality)
                ok += 1
                self.after(0, self.log, f"[完成 {i}/{total}] {os.path.basename(src)} → {os.path.basename(dst)}")
            except Exception as e:
                fail += 1
                self.after(0, self.log, f"[失败 {i}/{total}] {os.path.basename(src)}：{e}")

            self.after(0, self._set_progress, i)
            self.after(0, self.status_label.config, {"text": f"{i}/{total}  成功 {ok} 失败 {fail}"})

        self.after(0, self._finish, ok, fail, "图片")

    # ------------------------- 视频任务 -------------------------
    def run_video(self, files, container, target_ext, vcodec, acodec,
                  quality, output_dir, ffmpeg, ffprobe):
        self._set_busy(True)
        self.log("=" * 50)
        self.log(f"[视频] 开始转换 → {container}（视频 {vcodec} / 音频 {acodec}），画质 {quality}")
        threading.Thread(
            target=self._video_worker,
            args=(files, target_ext, vcodec, acodec, quality, output_dir, ffmpeg, ffprobe),
            daemon=True,
        ).start()

    def _video_worker(self, files, target_ext, vcodec, acodec, quality,
                      output_dir, ffmpeg, ffprobe):
        total = len(files)
        ok = fail = 0
        self.after(0, self._set_progress, 0, total)

        for i, src in enumerate(files, start=1):
            if self.cancel_flag:
                self.after(0, self.log, "[取消] 任务已中止")
                break
            try:
                if output_dir:
                    base = os.path.splitext(os.path.basename(src))[0]
                    dst = os.path.join(output_dir, base + target_ext)
                else:
                    dst = os.path.splitext(src)[0] + target_ext

                if os.path.abspath(src) == os.path.abspath(dst):
                    self.after(0, self.log, f"[跳过] 已是目标格式：{os.path.basename(src)}")
                    ok += 1
                    continue

                def on_progress(frac, idx=i):
                    self.after(0, self._set_progress, (idx - 1) + frac)

                transcode_one(src, dst, vcodec, acodec, quality, ffmpeg, ffprobe,
                              progress_cb=on_progress,
                              cancel_check=lambda: self.cancel_flag)
                ok += 1
                self.after(0, self.log, f"[完成 {i}/{total}] {os.path.basename(src)} → {os.path.basename(dst)}")
            except Exception as e:
                fail += 1
                self.after(0, self.log, f"[失败 {i}/{total}] {os.path.basename(src)}：{e}")

            self.after(0, self._set_progress, i)
            self.after(0, self.status_label.config, {"text": f"{i}/{total}  成功 {ok} 失败 {fail}"})

        self.after(0, self._finish, ok, fail, "视频")

    def _finish(self, ok, fail, kind):
        self._set_busy(False)
        self.log(f"{kind}转换结束：成功 {ok} 个，失败 {fail} 个。")
        if fail == 0:
            messagebox.showinfo("完成", f"全部转换成功！\n共 {ok} 个{kind}。")
        else:
            messagebox.showwarning("完成（有失败）", f"成功 {ok} 个，失败 {fail} 个，详见日志。")


# ---------------------------------------------------------------------------
# 命令行模式
# ---------------------------------------------------------------------------
def run_cli(argv):
    import argparse

    parser = argparse.ArgumentParser(description="图片 / 视频格式批量转换工具（命令行）")
    parser.add_argument("--mode", choices=["image", "video"], default="image",
                        help="转换模式：image（默认）/ video")
    parser.add_argument("--input", help="单个输入文件路径")
    parser.add_argument("--input-dir", help="输入文件夹（递归扫描）")
    parser.add_argument("--format", help="图片目标格式：jpg/png/bmp/gif/tiff/webp/ico/ppm/tga")
    parser.add_argument("--vformat", help="视频目标格式：mp4/mkv/mov/avi/wmv/flv/webm/ts/m4v/3gp/mpg")
    parser.add_argument("--output", help="输出文件路径（单文件时使用）")
    parser.add_argument("--output-dir", help="输出目录（批量时使用）")
    parser.add_argument("--quality", type=int, default=None, help="画质 1-100（图片默认 95，视频默认 80）")
    parser.add_argument("--ffmpeg", help="高级选项：显式指定 ffmpeg 路径（默认自动扫描 PATH 与程序目录）")
    args = parser.parse_args(argv)

    if args.mode == "video":
        return _cli_video(args)
    return _cli_image(args)


def _cli_image(args):
    if Image is None:
        print("错误：未安装 Pillow，请先运行 pip install Pillow")
        return 1
    if not args.format:
        print("错误：图片模式请用 --format 指定目标格式。")
        return 1

    fmt_map = {label.split()[0].lower(): v for label, v in OUTPUT_FORMATS.items()}
    fmt_map["jpg"] = ("JPEG", ".jpg")
    fmt_key = args.format.lower().lstrip(".")
    if fmt_key not in fmt_map:
        print(f"不支持的图片格式：{args.format}，可选：{', '.join(fmt_map)}")
        return 1
    target_format, target_ext = fmt_map[fmt_key]
    quality = args.quality or 95

    files = []
    if args.input:
        files.append(os.path.abspath(args.input))
    if args.input_dir:
        files += [os.path.abspath(p) for p in scan_by_ext(args.input_dir, INPUT_EXTS)]
    if not files:
        print("错误：请通过 --input 或 --input-dir 指定图片。")
        return 1

    out_dir = args.output_dir or (os.path.dirname(args.output) if args.output else "")
    ok = fail = 0
    for src in files:
        try:
            if args.output and len(files) == 1:
                dst = args.output
            else:
                base = os.path.splitext(os.path.basename(src))[0]
                dst = os.path.join(out_dir, base + target_ext) if out_dir \
                    else os.path.splitext(src)[0] + target_ext
            convert_one(src, dst, target_format, quality)
            print(f"[完成] {src} -> {dst}")
            ok += 1
        except Exception as e:
            print(f"[失败] {src}：{e}")
            fail += 1

    print(f"\n结束：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 2


def _cli_video(args):
    if not args.vformat:
        print("错误：视频模式请用 --vformat 指定目标格式。")
        return 1

    vfmt_map = {label.split()[0].lower(): (ext, vc, ac)
                for label, (ext, vc, ac) in VIDEO_FORMATS.items()}
    # 常用别名
    vfmt_map["ts"] = vfmt_map["mpeg-ts"]
    vfmt_map["mpegts"] = vfmt_map["mpeg-ts"]
    vfmt_map["mpg"] = vfmt_map["mpeg"]
    vfmt_map["m2p"] = vfmt_map["mpeg"]
    key = args.vformat.lower().lstrip(".")
    if key not in vfmt_map:
        print(f"不支持的视频格式：{args.vformat}，可选：{', '.join(vfmt_map)}")
        return 1
    container, vcodec, acodec = vfmt_map[key]
    target_ext = "." + container
    quality = args.quality or 80

    ffmpeg, ffprobe = find_ffmpeg_tools(args.ffmpeg)
    if not ffmpeg:
        print("错误：未找到 FFmpeg，请安装 FFmpeg 并加入 PATH，或用 --ffmpeg 指定路径。")
        return 1

    files = []
    if args.input:
        files.append(os.path.abspath(args.input))
    if args.input_dir:
        files += [os.path.abspath(p) for p in scan_by_ext(args.input_dir, VIDEO_INPUT_EXTS)]
    if not files:
        print("错误：请通过 --input 或 --input-dir 指定视频。")
        return 1

    out_dir = args.output_dir or (os.path.dirname(args.output) if args.output else "")
    ok = fail = 0
    for src in files:
        try:
            if args.output and len(files) == 1:
                dst = args.output
            else:
                base = os.path.splitext(os.path.basename(src))[0]
                dst = os.path.join(out_dir, base + target_ext) if out_dir \
                    else os.path.splitext(src)[0] + target_ext
            transcode_one(src, dst, vcodec, acodec, quality, ffmpeg, ffprobe)
            print(f"[完成] {src} -> {dst}")
            ok += 1
        except Exception as e:
            print(f"[失败] {src}：{e}")
            fail += 1

    print(f"\n结束：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(run_cli(sys.argv[1:]))
    else:
        app = ConverterApp()
        app.mainloop()
