#!/usr/bin/env python3
"""
⚡️ 用 ⚡️ 视 ⚡️ 频 ⚡️ 安 ⚡️ 装 ⚡️ 乐 ⚡️ 子 ⚡️
—— 任意 ISO 镜像 → 合成滚动视频（带 UI 界面）

将任意 ISO 镜像（Windows / Linux / macOS / 任何系统）中的所有文件
转为数据流滚动视频，合成为一个带完整 UI 的视频。

支持两种输入方式：
  1. 直接给 .iso 文件 → 自动挂载 → 处理 → 自动卸载
  2. 给已挂载的目录 → 直接扫描处理

UI 布局:
    ┌──────────────────────────────────────────────┐
    │            📀 Windows 11 24H2                │  ← 顶部：镜像名
    ├────────────┬─────────────────────────────────┤
    │ ▶ 正在播放  │                                 │
    │ setup.exe  │       数据流滚动视频（主画面）      │  ← 右侧：滚动画面
    │ ██████░░ 60%│                                │
    │────────────│                                 │
    │ 📁 文件列表 │                                 │
    │ autorun.inf│                                 │
    │▶bootmgr.efi│                                 │  ← 左侧：文件列表
    │ boot.wim   │                                 │
    │ install.wim│                                 │
    │ setup.exe  │                                 │
    ├────────────┴─────────────────────────────────┤
    │ 📄 sources/boot.wim                          │  ← 底部：当前文件路径
    └──────────────────────────────────────────────┘
"""

import os
import sys
import argparse
import time
import subprocess
import tempfile
import shutil
import platform
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# ISO 自动挂载 / 卸载
# ============================================================

_IS_MACOS = platform.system() == 'Darwin'
_IS_LINUX = platform.system() == 'Linux'


def mount_iso(iso_path):
    """
    自动挂载 ISO 文件，返回挂载点路径。
    支持 macOS (hdiutil) 和 Linux (mount -o loop)。
    返回 (mount_point, needs_cleanup)，其中 needs_cleanup 表示是否需要手动卸载。
    """
    iso_path = os.path.abspath(iso_path)

    if not os.path.isfile(iso_path):
        raise FileNotFoundError(f"ISO 文件不存在: {iso_path}")

    if _IS_MACOS:
        # macOS: hdiutil attach
        result = subprocess.run(
            ['hdiutil', 'attach', '-nobrowse', '-readonly', '-mount', 'required', iso_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"挂载 ISO 失败:\n{result.stderr}")

        # 解析输出找到挂载点
        mount_point = None
        for line in result.stdout.split('\n'):
            if '/Volumes/' in line:
                # 格式: /dev/disk4    /Volumes/XXX
                parts = line.strip().split('\t')
                for p in parts:
                    if p.startswith('/Volumes/'):
                        mount_point = p.strip()
                        break
            if mount_point:
                break

        if not mount_point or not os.path.isdir(mount_point):
            raise RuntimeError(f"无法解析挂载点:\n{result.stdout}")

        return mount_point, True

    elif _IS_LINUX:
        # Linux: mount -o loop
        mount_point = tempfile.mkdtemp(prefix='iso_mount_')
        try:
            result = subprocess.run(
                ['sudo', 'mount', '-o', 'loop,ro', iso_path, mount_point],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                os.rmdir(mount_point)
                raise RuntimeError(f"挂载 ISO 失败（需要 sudo）:\n{result.stderr}")
        except Exception:
            if os.path.isdir(mount_point):
                os.rmdir(mount_point)
            raise
        return mount_point, True

    else:
        raise OSError(f"当前系统 ({platform.system()}) 不支持自动挂载 ISO，请手动挂载后传入目录路径")


def unmount_iso(mount_point):
    """卸载已挂载的 ISO"""
    if _IS_MACOS:
        subprocess.run(['hdiutil', 'detach', mount_point],
                       capture_output=True, timeout=15)
    elif _IS_LINUX:
        subprocess.run(['sudo', 'umount', mount_point],
                       capture_output=True, timeout=15)
        try:
            os.rmdir(mount_point)
        except OSError:
            pass


def is_iso_file(path):
    """判断是否为 ISO 文件（通过扩展名和 Magic Number）"""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return False

    ext = os.path.splitext(path)[1].lower()
    if ext in ('.iso', '.img', '.dmg', '.cdr', '.udf', '.bin'):
        return True

    # 尝试读 Magic Number（ISO 9660 在偏移 32769 处有 "CD001"）
    try:
        with open(path, 'rb') as f:
            f.seek(32769)
            magic = f.read(5)
            if magic == b'CD001':
                return True
    except (IOError, OSError):
        pass

    return False


# ============================================================
# 配置常量
# ============================================================
FPS = 30
SECONDS_PER_FILE = 3                        # 每个文件播放秒数（默认）
MAX_READ_BYTES = None                       # None = 读取完整文件，不截断

# 画布尺寸
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080

# UI 布局参数
LEFT_PANEL_WIDTH = 400
TOP_BAR_HEIGHT = 76
BOTTOM_BAR_HEIGHT = 56

RIGHT_PANEL_X = LEFT_PANEL_WIDTH
RIGHT_PANEL_Y = TOP_BAR_HEIGHT
RIGHT_PANEL_WIDTH = VIDEO_WIDTH - LEFT_PANEL_WIDTH
RIGHT_PANEL_HEIGHT = VIDEO_HEIGHT - TOP_BAR_HEIGHT - BOTTOM_BAR_HEIGHT

# 颜色（RGB）
BG_COLOR = (15, 15, 15)
PANEL_BG = (26, 26, 28)
TOP_BAR_BG = (20, 20, 22)
BOTTOM_BAR_BG = (20, 20, 22)
TEXT_PRIMARY = (225, 225, 225)
TEXT_SECONDARY = (145, 145, 150)
ACCENT = (0, 195, 255)        # 青色强调
HIGHLIGHT_BG = (38, 55, 72)
PROGRESS_BG = (55, 55, 58)
PROGRESS_FILL = (0, 185, 110) # 绿色进度
SEPARATOR = (52, 52, 56)

ROW_HEIGHT = 22
LIST_ITEM_FONT_SIZE = 15

# ============================================================
# 字体
# ============================================================
_FONT_CACHE = {}

def _get_font(size, bold=False):
    cache_key = (size, bold)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    font_paths = [
        '/System/Library/Fonts/PingFang.ttc',         # macOS
        '/System/Library/Fonts/STHeiti Medium.ttc',   # macOS 备选
        '/System/Library/Fonts/Hiragino Sans GB.ttc', # macOS 备选2
        'C:\\Windows\\Fonts\\msyh.ttc',                # Windows
        'C:\\Windows\\Fonts\\simhei.ttf',              # Windows 备选
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', # Linux
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, size)
                _FONT_CACHE[cache_key] = font
                return font
            except Exception:
                continue

    # 兜底：默认字体（不支持中文，但不会崩溃）
    _FONT_CACHE[cache_key] = ImageFont.load_default()
    return _FONT_CACHE[cache_key]

# ============================================================
# 文件扫描
# ============================================================

def get_all_files(root_dir):
    """递归收集目录下所有文件，返回 [(相对路径, 绝对路径, 文件大小), ...]"""
    files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            if size == 0:
                continue  # 跳过空文件
            rel = os.path.relpath(full, root_dir)
            files.append((rel, full, size))
    return files


def read_file_head(filepath, max_bytes=MAX_READ_BYTES):
    """读取文件头部最多 max_bytes 字节，max_bytes=None 则读完整文件"""
    try:
        with open(filepath, 'rb') as f:
            if max_bytes is None:
                return f.read()
            return f.read(max_bytes)
    except (IOError, PermissionError, OSError) as e:
        return None


# ---- bsdtar 直读 ISO（无需挂载） ----

def _find_bsdtar():
    """查找可用的 bsdtar 或兼容工具"""
    for name in ['bsdtar', 'tar']:
        path = shutil.which(name)
        if path:
            return path
    return None


def get_all_files_from_bsdtar(iso_path):
    """
    用 bsdtar 列出 ISO 内所有文件。
    返回 [(archive_path, iso_path, size), ...]
    其中 archive_path 是 ISO 内的路径（如 './casper/filesystem.squashfs'）
    """
    bsdtar = _find_bsdtar()
    if not bsdtar:
        raise RuntimeError("未找到 bsdtar，无法读取 ISO 内容")

    result = subprocess.run(
        [bsdtar, '-tf', iso_path],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"bsdtar 读取文件列表失败:\n{result.stderr}")

    files = []
    for line in result.stdout.strip().split('\n'):
        path = line.strip()
        if not path or path == '.':
            continue
        # 跳过目录（bsdtar 列表中目录通常以 / 结尾或无后缀，但纯路径无法区分）
        # bsdtar -tf 列出的是完整路径，我们需要用 v 模式获取更多信息
        files.append((path, iso_path, -1))  # size 先用 -1 占位

    return files


def get_all_files_from_bsdtar_detailed(iso_path):
    """
    用 bsdtar -tvf 列出 ISO 内所有文件及大小。
    返回 [(archive_path, iso_path, size), ...]
    """
    bsdtar = _find_bsdtar()
    if not bsdtar:
        raise RuntimeError("未找到 bsdtar，无法读取 ISO 内容")

    result = subprocess.run(
        [bsdtar, '-tvf', iso_path],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        # fallback 到普通列表
        return get_all_files_from_bsdtar(iso_path)

    files = []
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        # bsdtar -tvf 输出格式: -rw-r--r--  0 root   root   12345 Dec 31  1979 path/to/file
        # 或: drwxr-xr-x  0 root   root       0 Dec 31  1979 path/to/dir
        parts = line.split()
        if len(parts) < 6 or parts[0].startswith('d'):
            continue  # 跳过目录

        try:
            size = int(parts[4])  # 第5列是大小
            # 路径是最后一个空格之后的部分
            path = parts[8] if len(parts) > 8 else parts[-1]
        except (ValueError, IndexError):
            continue

        if size == 0:
            continue

        files.append((path, iso_path, size))

    if not files:
        # 回退到简单列表
        return get_all_files_from_bsdtar(iso_path)

    return files


def read_file_head_from_bsdtar(iso_path, archive_path, max_bytes=MAX_READ_BYTES):
    """用 bsdtar 读取 ISO 内某个文件的头部数据，max_bytes=None 则读完整文件"""
    bsdtar = _find_bsdtar()
    if not bsdtar:
        return None

    try:
        result = subprocess.run(
            [bsdtar, '-xOf', iso_path, archive_path],
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            return None
        if max_bytes is None:
            return result.stdout
        return result.stdout[:max_bytes]
    except (subprocess.TimeoutExpired, Exception):
        return None


def try_mount_iso(iso_path):
    """
    尝试挂载 ISO，成功返回 (mount_point, needs_unmount)。
    失败返回 (None, False)（调用方应改用 bsdtar）。
    """
    try:
        mount_point, needs_unmount = mount_iso(iso_path)
        # 验证挂载点有文件
        if os.path.isdir(mount_point) and any(os.scandir(mount_point)):
            return mount_point, needs_unmount
        else:
            if needs_unmount:
                unmount_iso(mount_point)
            return None, False
    except Exception:
        return None, False


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.1f}K"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes/1024/1024:.1f}M"
    else:
        return f"{size_bytes/1024/1024/1024:.2f}G"


# ============================================================
# 滚动视频帧生成
# ============================================================

def prepare_scroll_data(raw_data, frame_bytes):
    """将原始数据填充到至少一帧大小，返回 bytes"""
    if len(raw_data) >= frame_bytes:
        return raw_data[:frame_bytes]
    padded = bytearray()
    while len(padded) < frame_bytes:
        padded.extend(raw_data)
    return bytes(padded[:frame_bytes])


def create_scroll_frame(data, frame_index, width, height):
    """从二进制数据生成滚动视频的一帧（BGR numpy array）"""
    bytes_per_frame = width * height * 3
    data_size = len(data)
    step = width * 3  # 每帧移动一行像素

    start = (frame_index * step) % data_size
    chunk = data[start:start + bytes_per_frame]
    if len(chunk) < bytes_per_frame:
        chunk += data[:bytes_per_frame - len(chunk)]

    return np.frombuffer(chunk, dtype=np.uint8).reshape((height, width, 3))


# ============================================================
# ============================================================
# UI 绘制（核心）—— 优化版：静态 UI 预渲染 + 动态叠加
# ============================================================

def render_static_ui_bgr(mirror_name, all_files, current_idx, current_relpath):
    """
    预渲染静态 UI（每文件只调用一次）→ 返回 BGR numpy array。
    PIL 绘制中文 UI，转 BGR 一次。
    """
    canvas = Image.new('RGB', (VIDEO_WIDTH, VIDEO_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    f_title = _get_font(28, bold=True)
    f_file = _get_font(18)
    f_list = _get_font(LIST_ITEM_FONT_SIZE)
    f_small = _get_font(13)

    bottom_y = VIDEO_HEIGHT - BOTTOM_BAR_HEIGHT

    # === 顶栏 ===
    draw.rectangle([(0, 0), (VIDEO_WIDTH, TOP_BAR_HEIGHT)], fill=TOP_BAR_BG)
    draw.line([(0, TOP_BAR_HEIGHT - 1), (VIDEO_WIDTH, TOP_BAR_HEIGHT - 1)], fill=SEPARATOR, width=1)
    title = f"📀 {mirror_name}"
    tw = draw.textlength(title, font=f_title)
    draw.text(((VIDEO_WIDTH - tw) // 2, 22), title, fill=TEXT_PRIMARY, font=f_title)

    # === 底栏 ===
    draw.rectangle([(0, bottom_y), (VIDEO_WIDTH, VIDEO_HEIGHT)], fill=BOTTOM_BAR_BG)
    draw.line([(0, bottom_y), (VIDEO_WIDTH, bottom_y)], fill=SEPARATOR, width=1)
    draw.text((16, bottom_y + 18), f"📄 {current_relpath}", fill=TEXT_PRIMARY, font=f_file)

    # === 左侧面板背景 ===
    draw.rectangle([(0, TOP_BAR_HEIGHT), (LEFT_PANEL_WIDTH, bottom_y)], fill=PANEL_BG)
    draw.line([(LEFT_PANEL_WIDTH - 1, TOP_BAR_HEIGHT), (LEFT_PANEL_WIDTH - 1, bottom_y)],
              fill=SEPARATOR, width=1)

    # === 文件信息区 ===
    y = TOP_BAR_HEIGHT + 12
    draw.text((12, y), "▶ 正在播放", fill=ACCENT, font=f_small)
    y += 20
    display_name = current_relpath
    max_chars = 34
    if len(display_name) > max_chars:
        display_name = "…" + display_name[-(max_chars - 1):]
    draw.text((12, y), display_name, fill=TEXT_PRIMARY, font=f_file)

    info_end_y = TOP_BAR_HEIGHT + 120
    draw.line([(8, info_end_y - 4), (LEFT_PANEL_WIDTH - 8, info_end_y - 4)], fill=SEPARATOR, width=1)

    # === 文件列表 ===
    list_hdr_y = info_end_y
    draw.text((12, list_hdr_y + 2), f"📁 文件列表 ({len(all_files)})",
              fill=TEXT_SECONDARY, font=f_small)
    list_start_y = list_hdr_y + 22
    list_area_h = bottom_y - list_start_y - 8
    visible_rows = list_area_h // ROW_HEIGHT
    offset = current_idx - visible_rows // 3
    offset = max(0, min(offset, len(all_files) - visible_rows))

    for i in range(visible_rows):
        fi = offset + i
        if fi >= len(all_files):
            break
        rel, _, sz = all_files[fi]
        row_y = list_start_y + i * ROW_HEIGHT
        is_current = (fi == current_idx)

        if is_current:
            draw.rectangle([(6, row_y - 1), (LEFT_PANEL_WIDTH - 6, row_y + ROW_HEIGHT - 1)],
                           fill=HIGHLIGHT_BG)
            draw.text((14, row_y + 2), "▶", fill=ACCENT, font=f_small)
            name_x, tc = 28, TEXT_PRIMARY
        else:
            name_x, tc = 14, TEXT_SECONDARY

        name = rel
        available_w = LEFT_PANEL_WIDTH - name_x - 58
        while draw.textlength(name, font=f_list) > available_w and len(name) > 4:
            name = "…" + name[-(len(name) - 3):]
        draw.text((name_x, row_y + 3), name, fill=tc, font=f_list)
        size_str = format_size(sz)
        stw = draw.textlength(size_str, font=f_small)
        draw.text((LEFT_PANEL_WIDTH - 12 - stw, row_y + 4),
                  size_str, fill=tc if is_current else TEXT_SECONDARY, font=f_small)

    # PIL RGB → BGR numpy（一次）
    return cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)


def apply_dynamic_overlay(static_bgr, scroll_frame_bgr,
                          current_idx, current_progress,
                          frames_done, frames_total,
                          all_files):
    """
    动态叠加：纯 numpy + OpenCV，零色彩转换，零 PIL 调用。
    """
    result = static_bgr.copy()
    result[RIGHT_PANEL_Y:RIGHT_PANEL_Y + RIGHT_PANEL_HEIGHT,
           RIGHT_PANEL_X:RIGHT_PANEL_X + RIGHT_PANEL_WIDTH] = scroll_frame_bgr

    # BGR 颜色常量
    T_C = TEXT_SECONDARY[::-1]

    # === 进度条 ===
    bar_y = TOP_BAR_HEIGHT + 12 + 20 + 26 + 4
    bar_x, bar_w, bar_h = 12, LEFT_PANEL_WIDTH - 24, 10
    cv2.rectangle(result, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), PROGRESS_BG[::-1], -1)
    fill_w = int(bar_w * current_progress)
    if fill_w > 0:
        cv2.rectangle(result, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), PROGRESS_FILL[::-1], -1)

    pct_y = bar_y + bar_h + 16
    cv2.putText(result, f"{current_progress*100:.0f}%  {frames_done}/{frames_total}",
                (14, pct_y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, T_C, 1, cv2.LINE_8)

    time_y = pct_y + 18
    remaining_sec = (frames_total - frames_done) / FPS
    cv2.putText(result, f"{remaining_sec:.1f}s left",
                (14, time_y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, T_C, 1, cv2.LINE_8)

    # === 底栏全局进度 ===
    bottom_y = VIDEO_HEIGHT - BOTTOM_BAR_HEIGHT
    overall_pct = (current_idx + current_progress) / len(all_files) * 100
    overall_text = f"Total: {overall_pct:.1f}%  [{current_idx+1}/{len(all_files)}]"
    (tw, th), _ = cv2.getTextSize(overall_text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    cv2.rectangle(result, (VIDEO_WIDTH - tw - 30, bottom_y + 10),
                  (VIDEO_WIDTH - 8, bottom_y + BOTTOM_BAR_HEIGHT - 6),
                  BOTTOM_BAR_BG[::-1], -1)
    cv2.putText(result, overall_text, (VIDEO_WIDTH - tw - 24, bottom_y + 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, T_C, 1, cv2.LINE_8)

    return result


def render_static_ui(mirror_name, all_files, current_idx, current_relpath):
    """兼容旧接口"""
    return render_static_ui_bgr(mirror_name, all_files, current_idx, current_relpath)


def draw_composite_frame(scroll_frame_bgr, mirror_name, all_files,
                         current_idx, current_progress,
                         frames_done, frames_total,
                         current_relpath):
    """兼容旧接口"""
    static = render_static_ui_bgr(mirror_name, all_files, current_idx, current_relpath)
    return apply_dynamic_overlay(static, scroll_frame_bgr,
                                 current_idx, current_progress,
                                 frames_done, frames_total, all_files)


# ============================================================
# 主流程
# ============================================================

def create_composite_video(source_dir, output_path, mirror_name=None,
                           seconds_per_file=SECONDS_PER_FILE, fps=FPS,
                           max_files=None, iso_path=None):
    """
    主函数：扫描目录 → 逐文件编码滚动视频帧 → 合成带 UI 的视频

    适用任意 ISO 镜像（Windows / Linux / macOS / 任何操作系统），
    也适用任何目录。每个文件读取完整数据转成滚动画面。

    参数:
        source_dir:        源目录（ISO 挂载目录或任意文件夹）
        output_path:       输出视频路径（不含扩展名，自动选编码器）
        mirror_name:       镜像名称（显示在视频顶部）
        seconds_per_file:  每个文件播放秒数
        fps:               帧率
        max_files:         最多处理文件数（None = 全部）
        iso_path:          原始 ISO 文件路径（bsdtar 直读模式使用）
    """
    if mirror_name is None:
        mirror_name = os.path.basename(source_dir.rstrip('/\\'))

    frames_per_file = int(seconds_per_file * fps)
    scroll_frame_bytes = RIGHT_PANEL_WIDTH * RIGHT_PANEL_HEIGHT * 3

    # ---- 1. 扫描文件 ----
    use_bsdtar = iso_path is not None

    if use_bsdtar:
        print("\n" + "=" * 64)
        print(f"  💿 bsdtar 直读 ISO: {iso_path}")
        print("=" * 64)
        t0 = time.time()

        all_files = get_all_files_from_bsdtar_detailed(iso_path)
        all_files.sort(key=lambda x: x[0].lower())
    else:
        print("\n" + "=" * 64)
        print(f"  🔍 扫描目录: {source_dir}")
        print("=" * 64)
        t0 = time.time()

        all_files = get_all_files(source_dir)
        all_files.sort(key=lambda x: x[0].lower())

    if max_files and len(all_files) > max_files:
        all_files = all_files[:max_files]
        print(f"  ⚠️ 文件过多，只取前 {max_files} 个")

    # 统计大小（bsdtar 模式可能部分文件 size 为 -1）
    sized = [f[2] for f in all_files if f[2] > 0]
    avg_size = sum(sized) / len(sized) if sized else 0
    total_size_known = sum(f[2] for f in all_files if f[2] > 0)
    unknown_count = sum(1 for f in all_files if f[2] <= 0)

    print(f"  ✅ 找到 {len(all_files)} 个文件"
          + (f"，已知总大小 {format_size(total_size_known)}" if total_size_known > 0 else "")
          + (f"，{unknown_count} 个未知大小" if unknown_count > 0 else ""))
    print(f"  每文件播放 {seconds_per_file}s，共 {len(all_files)*seconds_per_file/60:.1f} 分钟")
    if use_bsdtar:
        print(f"  读取方式: bsdtar 直读（无需挂载）")

    if not all_files:
        print("  ❌ 没有找到任何文件！")
        return False

    # ---- 2. 选择编码器（H.264 优先，比 XVID 小 10-50 倍） ----
    codecs = [
        ('avc1', '.mov'),    # H.264 — 广泛兼容，体积小
        ('hvc1', '.mov'),    # HEVC (H.265) — 更高压缩比
        ('X264', '.mkv'),    # x264 开源 H.264 — 体积最小
        ('mp4v', '.mp4'),    # MPEG-4 — 备选
        ('XVID', '.avi'),    # 兜底
        ('DIB ', '.avi'),    # 无损兜底
    ]
    out, out_file = None, None
    for codec, ext in codecs:
        base, _ = os.path.splitext(output_path)
        out_file = base + ext
        fourcc = cv2.VideoWriter_fourcc(*codec)
        out = cv2.VideoWriter(out_file, fourcc, fps, (VIDEO_WIDTH, VIDEO_HEIGHT))
        if out.isOpened():
            print(f"  ✅ 编码器: {codec.strip()} → {out_file}")
            break
        out.release()
        out = None

    if out is None:
        print("  ❌ 没有可用编码器，请安装 opencv-python")
        return False

    # ---- 3. 逐文件编码 ----
    total_all_frames = len(all_files) * frames_per_file
    global_frame = 0

    print(f"\n  📹 开始编码复合视频...")
    sep_line = "─" * 56
    print(f"  {sep_line}")
    print(f"  总帧数: {total_all_frames}  |  总时长: {total_all_frames/fps/60:.1f} 分钟")
    print(f"  分辨率: {VIDEO_WIDTH}×{VIDEO_HEIGHT}")
    print(f"  滚动区: {RIGHT_PANEL_WIDTH}×{RIGHT_PANEL_HEIGHT}")
    print(f"  {sep_line}\n")

    blank = np.zeros((RIGHT_PANEL_HEIGHT, RIGHT_PANEL_WIDTH, 3), dtype=np.uint8)

    for file_idx, (rel_path, full_path, file_size) in enumerate(all_files):
        file_start = time.time()

        # 进度指示
        bar_len = 30
        done = int(bar_len * file_idx / len(all_files))
        file_bar = "█" * done + "░" * (bar_len - done)
        size_str = format_size(file_size) if file_size > 0 else "未知"
        print(f"  [{file_bar}] {file_idx+1:>4}/{len(all_files)}  "
              f"{size_str:>8}  {rel_path}")

        # 读取数据
        if use_bsdtar:
            raw_data = read_file_head_from_bsdtar(iso_path, rel_path)
        else:
            raw_data = read_file_head(full_path)

        # 预渲染静态 UI（每文件一次）
        static_ui = render_static_ui(mirror_name, all_files, file_idx, rel_path)

        if raw_data is None or len(raw_data) == 0:
            # 无法读取 → 空白滚动
            print(f"         ⚠️ 跳过（无法读取），填充空白段")
            for fi in range(frames_per_file):
                progress = fi / frames_per_file
                composite = apply_dynamic_overlay(
                    static_ui, blank.copy(),
                    file_idx, progress, fi + 1, frames_per_file, all_files
                )
                out.write(composite)
                global_frame += 1
            continue

        print(f"         读取 {format_size(len(raw_data))}")

        # 准备滚动数据
        scroll_data = prepare_scroll_data(raw_data, scroll_frame_bytes)
        data_size = len(scroll_data)
        unique_frames = data_size // (RIGHT_PANEL_WIDTH * 3)
        print(f"         滚动数据 {format_size(data_size)}，~{unique_frames} 个唯一帧")

        # 逐帧生成（复用静态 UI，仅叠加动态内容）
        for fi in range(frames_per_file):
            progress = fi / frames_per_file

            scroll_frame = create_scroll_frame(
                scroll_data, fi, RIGHT_PANEL_WIDTH, RIGHT_PANEL_HEIGHT
            )

            composite = apply_dynamic_overlay(
                static_ui, scroll_frame,
                file_idx, progress, fi + 1, frames_per_file, all_files
            )

            out.write(composite)
            global_frame += 1

        elapsed = time.time() - file_start
        print(f"         ✓ 完成 ({elapsed:.1f}s)")

    # ---- 4. 收尾 ----
    out.release()
    total_elapsed = time.time() - t0
    video_size_mb = os.path.getsize(out_file) / (1024 * 1024)

    print(f"\n  {'=' * 56}")
    print(f"  ✅ 视频生成成功！")
    print(f"  {'=' * 56}")
    print(f"  📁 {out_file}")
    print(f"  📊 {video_size_mb:.1f} MB")
    print(f"  ⏱️  时长 {total_all_frames/fps/60:.1f} 分钟")
    print(f"  📁 {len(all_files)} 个文件")
    print(f"  🎬 {total_all_frames} 帧 @ {fps} fps")
    print(f"  ⏱️  耗时 {total_elapsed/60:.1f} 分钟")
    print(f"  {'=' * 56}\n")

    return True


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="任意 ISO 镜像 → 合成滚动视频（带 UI 界面）\n"
                    "支持 .iso / .img / .dmg 等，也可传入已挂载目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python iso_composite.py Win11_24H2.iso                           # 直接拖 ISO
  python iso_composite.py ubuntu-24.04.iso -n "Ubuntu 24.04" -s 5  # Linux 镜像
  python iso_composite.py macOS.iso --max-files 100                # 只取前100个文件
  python iso_composite.py /Volumes/CCCOMA_X64FRE_ZH-CN_DV9         # 已挂载目录
  python iso_composite.py /mnt/iso -s 2 --fps 24 -o my_video       # 自定义参数
        """
    )
    parser.add_argument('source', help='ISO 文件路径 或 已挂载的目录路径')
    parser.add_argument('-o', '--output', default=None,
                        help='输出视频路径（不含扩展名），默认保存在脚本同目录')
    parser.add_argument('-n', '--name', default=None,
                        help='镜像名称（显示在顶部），默认取文件名/目录名')
    parser.add_argument('-s', '--seconds', type=float, default=SECONDS_PER_FILE,
                        help=f'每个文件播放秒数，默认 {SECONDS_PER_FILE}')
    parser.add_argument('--fps', type=int, default=FPS,
                        help=f'帧率，默认 {FPS}')
    parser.add_argument('--max-files', type=int, default=None,
                        help='最多处理的文件数（测试用）')

    args = parser.parse_args()

    # 默认输出到脚本同目录
    if args.output is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(script_dir, 'iso_composite')

    # ---- 判断输入类型 ----
    source = os.path.abspath(args.source)
    mount_point = None
    needs_unmount = False
    iso_path_for_bsdtar = None  # bsdtar 直读模式

    if is_iso_file(source):
        # === ISO 文件：先尝试挂载，失败则用 bsdtar ===
        print(f"\n  💿 检测到 ISO 文件")

        # 自动生成名称
        if args.name is None:
            args.name = os.path.splitext(os.path.basename(source))[0]

        # 尝试挂载
        print(f"  🔌 尝试挂载...")
        mount_point, needs_unmount = try_mount_iso(source)

        if mount_point and os.path.isdir(mount_point):
            print(f"  ✅ 已挂载到: {mount_point}")
            source_dir = mount_point
        else:
            # 挂载失败 → bsdtar 直读
            print(f"  ⚠️ 挂载失败或不完整，切换到 bsdtar 直读模式")
            if _find_bsdtar():
                print(f"  ✅ 使用 bsdtar 读取 ISO 内容")
                source_dir = source  # 仅用于显示
                iso_path_for_bsdtar = source
            else:
                print(f"  ❌ 未找到 bsdtar，无法读取 ISO 内容")
                print(f"     请安装: brew install libarchive")
                sys.exit(1)

    elif os.path.isdir(source):
        # === 已挂载目录 ===
        source_dir = source
        if args.name is None:
            args.name = os.path.basename(source.rstrip('/\\'))

    else:
        print(f"❌ 输入无效: {source}")
        print("   请提供 .iso 文件路径 或 已挂载的目录路径")
        sys.exit(1)

    # ---- 执行 ----
    try:
        success = create_composite_video(
            source_dir=source_dir,
            output_path=args.output,
            mirror_name=args.name,
            seconds_per_file=args.seconds,
            fps=args.fps,
            max_files=args.max_files,
            iso_path=iso_path_for_bsdtar,
        )
    finally:
        # 确保卸载
        if needs_unmount and mount_point:
            print(f"  🔌 正在卸载 {mount_point} ...")
            unmount_iso(mount_point)
            print(f"  ✅ 已卸载")

    sys.exit(0 if success else 1)
