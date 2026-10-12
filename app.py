"""Battery — a pill battery on the desktop, with no background.

Built like the clock widget: the face is drawn with pillow (surface.py) and handed to Windows
as a per-pixel alpha layered window, so only the pill and its text are painted and every other
pixel is the desktop.  The window is owned by the shell's desktop window and pinned to the
bottom of the Z-order, so it survives Show Desktop and never floats over the user's apps.

While charging it animates: the fill sweeps up when the charger goes in, a shimmer runs along
it and the bolt breathes.  At a critical level on battery the fill pulses.  Otherwise it sits
still and redraws only when something changes.

    python app.py                   the widget
    python app.py --self-check      what it reads and would draw, without a window
    python app.py --render out.png  a preview sheet of the states
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import power
import surface

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "widget.log"

WINDOW_TITLE = "Battery"
CLASS_NAME = "BatteryPillWidget"

#: Base tick (hover-free housekeeping) and the animation tick.
TICK_MS = 250
FRAME_MS = 33
#: Power is polled every 2 s as a fallback; WM_POWERBROADCAST makes plug events instant.
POWER_TICKS = 8
INK_TICKS = 8
DESKTOP_TICKS = 2

INK_DARK_ABOVE = 0.55
INK_LIGHT_BELOW = 0.42

#: Animation timings, in seconds.
EASE_TAU = 0.22
SHIMMER_PERIOD = 2.4
BOLT_PERIOD = 1.8
BOLT_FADE = 0.3
FLASH_DECAY = 0.7
CRITICAL_PERIOD = 1.4

SIZES = {
    "small": ("Small", 200),
    "medium": ("Medium", 275),
    "large": ("Large", 360),
    "xlarge": ("Extra large", 480),
}
SIZE_KEYS = list(SIZES)
MIN_CSS, MAX_CSS = 150, 700
INKS = {"auto": "Auto (follow wallpaper)", "light": "Light", "dark": "Dark"}
INK_KEYS = list(INKS)

# ------------------------------------------------------------------ Win32 ---

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = (
    0x00080000, 0x00000080, 0x00000008, 0x08000000)
SW_HIDE, SW_SHOWNA, SW_RESTORE = 0, 8, 9
HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HTCAPTION, HTCLIENT, HTTRANSPARENT = 2, 1, -1
WM_DESTROY, WM_SIZE, WM_WINDOWPOSCHANGING, WM_NCHITTEST = 0x2, 0x5, 0x46, 0x84
WM_RBUTTONUP, WM_NCRBUTTONUP, WM_MOUSEWHEEL = 0x205, 0xA5, 0x20A
WM_NCLBUTTONDOWN, WM_SYSCOMMAND, WM_TIMER, WM_POWERBROADCAST = 0xA1, 0x112, 0x113, 0x218
WM_EXITSIZEMOVE, WM_NULL, WM_DISPLAYCHANGE, WM_DPICHANGED = 0x232, 0x0, 0x7E, 0x2E0
WM_APP_MENU, WM_APP_SHOW = 0x8001, 0x8002
SC_MINIMIZE, SC_MASK = 0xF020, 0xFFF0
SIZE_MINIMIZED = 1
MK_CONTROL = 0x8
AC_SRC_OVER, AC_SRC_ALPHA, ULW_ALPHA = 0, 1, 2
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
MF_STRING, MF_CHECKED, MF_SEPARATOR, MF_POPUP = 0x0, 0x8, 0x800, 0x10
MONITOR_DEFAULTTONEAREST = 2
IDC_ARROW = 32512
ERROR_CLASS_ALREADY_EXISTS = 1410
GW_OWNER, GWLP_HWNDPARENT = 4, -8
TIMER_TICK, TIMER_FRAME = 1, 2

MENU_SHOW_TIME, MENU_ON_TOP, MENU_DRAGGABLE, MENU_AUTOSTART = range(1, 5)
MENU_FOLDER, MENU_HIDE, MENU_QUIT = range(5, 8)
MENU_SIZE_BASE, MENU_INK_BASE = 100, 200

LRESULT = ctypes.c_ssize_t


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("hwndInsertAfter", ctypes.c_void_p),
                ("x", ctypes.c_int), ("y", ctypes.c_int), ("cx", ctypes.c_int),
                ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM)

_H, _I, _U, _P = wintypes.HWND, ctypes.c_int, wintypes.UINT, ctypes.c_void_p
for _dll, _name, _args, _res in (
    (user32, "GetDC", [_H], wintypes.HDC),
    (user32, "ReleaseDC", [_H, wintypes.HDC], _I),
    (user32, "GetDpiForWindow", [_H], _U),
    (user32, "MonitorFromWindow", [_H, wintypes.DWORD], _P),
    (user32, "GetMonitorInfoW", [_P, _P], wintypes.BOOL),
    (user32, "GetCursorPos", [_P], wintypes.BOOL),
    (user32, "GetAsyncKeyState", [_I], ctypes.c_short),
    (user32, "ShowWindow", [_H, _I], wintypes.BOOL),
    (user32, "IsIconic", [_H], wintypes.BOOL),
    (user32, "IsWindowVisible", [_H], wintypes.BOOL),
    (user32, "GetMessageW", [_P, _H, _U, _U], _I),
    (user32, "TranslateMessage", [_P], wintypes.BOOL),
    (user32, "DispatchMessageW", [_P], LRESULT),
    (user32, "PostQuitMessage", [_I], None),
    (user32, "PostMessageW", [_H, _U, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
    (user32, "SetWindowPos", [_H, _H, _I, _I, _I, _I, _U], wintypes.BOOL),
    (user32, "GetWindowRect", [_H, _P], wintypes.BOOL),
    (user32, "SetTimer", [_H, ctypes.c_size_t, _U, _P], ctypes.c_size_t),
    (user32, "KillTimer", [_H, ctypes.c_size_t], wintypes.BOOL),
    (user32, "SetForegroundWindow", [_H], wintypes.BOOL),
    (user32, "CreatePopupMenu", [], wintypes.HMENU),
    (user32, "DestroyMenu", [wintypes.HMENU], wintypes.BOOL),
    (user32, "AppendMenuW", [wintypes.HMENU, _U, ctypes.c_size_t, wintypes.LPCWSTR],
     wintypes.BOOL),
    (user32, "TrackPopupMenu", [wintypes.HMENU, _U, _I, _I, _I, _H, _P], _I),
    (user32, "LoadCursorW", [wintypes.HINSTANCE, _P], wintypes.HANDLE),
    (user32, "UpdateLayeredWindow", [_H, wintypes.HDC, _P, _P, wintypes.HDC, _P,
                                     wintypes.DWORD, _P, wintypes.DWORD], wintypes.BOOL),
    (user32, "DefWindowProcW", [_H, _U, wintypes.WPARAM, wintypes.LPARAM], LRESULT),
    (user32, "RegisterClassW", [_P], wintypes.WORD),
    (user32, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                 wintypes.DWORD, _I, _I, _I, _I, _H, wintypes.HMENU,
                                 wintypes.HINSTANCE, _P], _H),
    (user32, "FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR], _H),
    (user32, "GetWindow", [_H, _U], _H),
    (user32, "SetWindowLongPtrW", [_H, _I, _P], _P),
    (user32, "GetSystemMetrics", [_I], _I),
    (gdi32, "GetPixel", [wintypes.HDC, _I, _I], wintypes.DWORD),
    (gdi32, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
    (gdi32, "CreateDIBSection", [wintypes.HDC, _P, _U, _P, wintypes.HANDLE, wintypes.DWORD],
     wintypes.HBITMAP),
    (gdi32, "SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
    (gdi32, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
    (gdi32, "DeleteDC", [wintypes.HDC], wintypes.BOOL),
    (kernel32, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HINSTANCE),
):
    _func = getattr(_dll, _name)
    _func.argtypes = _args
    _func.restype = _res


# ------------------------------------------------------------------- util ---

DEBUG = False


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if DEBUG:
        print(f"{stamp}  {message}", flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {message}\n")
    except OSError:
        pass


def make_dpi_aware() -> None:
    try:
        ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def load_config() -> dict:
    config = {
        "on_top": False,
        "draggable": True,
        "show_time": True,
        "size": "medium",
        "width": SIZES["medium"][1],
        "ink": "auto",
        "window": None,
    }
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            config.update(saved)
    except (OSError, ValueError):
        pass
    return config


def save_config(config: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        log(f"could not write {CONFIG_FILE.name}: {error}")


def luminance(colour: int) -> float:
    def channel(value: int) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = colour & 0xFF, (colour >> 8) & 0xFF, (colour >> 16) & 0xFF
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def work_area(hwnd: int = 0) -> tuple[int, int, int, int]:
    monitor = user32.MonitorFromWindow(hwnd or None, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def startup_shortcut() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Battery.lnk"


def set_autostart(enabled: bool) -> bool:
    link = startup_shortcut()
    if not enabled:
        link.unlink(missing_ok=True)
        return True
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{exe}';$s.Arguments='\"{script}\"';$s.WorkingDirectory='{cwd}';$s.Save()"
    ).format(link=link, exe=exe, script=HERE / "app.py", cwd=HERE)
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        log(f"could not create the startup shortcut: {error}")
    return link.exists()


def tray_image(size: int = 64) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 18, 54, 46), radius=8, outline=(255, 255, 255, 255), width=4)
    draw.rounded_rectangle((56, 26, 61, 38), radius=2, fill=(255, 255, 255, 255))
    draw.rounded_rectangle((10, 24, 38, 40), radius=4, fill=(112, 216, 122, 255))
    return image


def smooth(phase: float) -> float:
    """Ease-in-out on 0-1, so the shimmer glides rather than ticks across."""
    return 0.5 - 0.5 * math.cos(math.pi * phase)


# ----------------------------------------------------------------- widget ---


class Widget:
    def __init__(self) -> None:
        self.config = load_config()
        self.hwnd = 0
        self.ink = "light" if self.config.get("ink") == "auto" else self.config["ink"]
        self.picture: surface.Render | None = None
        self.icon: pystray.Icon | None = None
        self._wndproc = WNDPROC(self._handle_message)
        self._tick = 0
        self.rate = power.ChargeRate()
        self.reading = power.read()
        #: What is on screen, as opposed to what the battery says: these ease.
        #: Starts empty, so the fill sweeps up to the level when the widget appears.
        self.shown_level = 0.0
        self.bolt = 1.0 if self.reading.plugged else 0.0
        self.flash = 0.0
        self.anim_start = time.monotonic()
        self.last_frame = self.anim_start
        self.animating = False

    # ------------------------------------------------------------ geometry

    @property
    def scale(self) -> float:
        dpi = user32.GetDpiForWindow(self.hwnd) if self.hwnd else 96
        return (dpi or 96) / 96.0

    def rect(self) -> tuple[int, int, int, int]:
        box = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(box))
        return box.left, box.top, box.right - box.left, box.bottom - box.top

    def width_css(self) -> float:
        return float(max(MIN_CSS, min(MAX_CSS, self.config.get("width", 275))))

    def apply_size(self, keep_position: bool = True) -> None:
        width, height, _ = surface.size_for(self.width_css(), self.scale)
        left, top, _, _ = self.rect()
        user32.SetWindowPos(self.hwnd, None, left, top, width, height,
                            SWP_NOZORDER | SWP_NOACTIVATE)
        self.draw()

    def save_geometry(self) -> None:
        left, top, _, _ = self.rect()
        self.config["window"] = {"x": int(round(left / self.scale)),
                                 "y": int(round(top / self.scale))}
        save_config(self.config)

    def place(self) -> None:
        scale = self.scale
        width, height, _ = surface.size_for(self.width_css(), scale)
        saved = self.config.get("window")
        left, top, right, bottom = work_area(self.hwnd)
        if isinstance(saved, dict) and "x" in saved and "y" in saved:
            x, y = int(saved["x"] * scale), int(saved["y"] * scale)
        else:
            x, y = right - width - int(16 * scale), bottom - height - int(16 * scale)
        # Never off-screen: a saved spot from a bigger monitor would lose the widget.
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        user32.SetWindowPos(self.hwnd, None, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE)

    # ------------------------------------------------------------- drawing

    def paint(self, picture: surface.Render) -> None:
        left, top, _, _ = self.rect()
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        header = BITMAPINFO()
        header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.bmiHeader.biWidth = picture.width
        header.bmiHeader.biHeight = -picture.height
        header.bmiHeader.biPlanes = 1
        header.bmiHeader.biBitCount = 32
        header.bmiHeader.biSizeImage = picture.width * picture.height * 4
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(memory, ctypes.byref(header), 0, ctypes.byref(bits),
                                        None, 0)
        if bitmap and bits:
            previous = gdi32.SelectObject(memory, bitmap)
            ctypes.memmove(bits, picture.pixels, len(picture.pixels))
            size = wintypes.SIZE(picture.width, picture.height)
            source = wintypes.POINT(0, 0)
            destination = wintypes.POINT(left, top)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            if not user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(destination),
                                              ctypes.byref(size), memory, ctypes.byref(source),
                                              0, ctypes.byref(blend), ULW_ALPHA):
                log(f"UpdateLayeredWindow failed ({ctypes.get_last_error()})")
            gdi32.SelectObject(memory, previous)
            gdi32.DeleteObject(bitmap)
        else:
            log("could not create a bitmap for the face")
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)

    def frame(self, now: float) -> surface.Frame:
        width, height, unit = surface.size_for(self.width_css(), self.scale)
        reading = self.reading
        t = now - self.anim_start
        charging = reading.charging
        shimmer = smooth((t % SHIMMER_PERIOD) / SHIMMER_PERIOD) if charging else None
        bolt = self.bolt
        if charging:
            bolt *= 0.78 + 0.22 * math.cos(2 * math.pi * t / BOLT_PERIOD)
        fill_opacity = 1.0
        if reading.level == "critical" and not reading.plugged:
            fill_opacity = 0.55 + 0.45 * (0.5 + 0.5 * math.cos(2 * math.pi * t / CRITICAL_PERIOD))
        percent = reading.percent
        to_full = self.rate.seconds_to_full(percent) if charging and percent is not None else None
        return surface.Frame(
            width=width, height=height, unit=unit, ink=self.ink,
            level=self.shown_level, colour=reading.level,
            percent_text="--" if percent is None else f"{percent}%",
            status_text=power.status_line(reading, to_full,
                                          bool(self.config.get("show_time", True))),
            charging=charging, shimmer=shimmer, bolt=bolt, flash=self.flash,
            fill_opacity=fill_opacity,
        )

    def draw(self) -> None:
        if not self.hwnd:
            return
        picture = surface.render(self.frame(time.monotonic()))
        self.picture = picture
        self.paint(picture)

    # ----------------------------------------------------------- animation

    def wants_animation(self) -> bool:
        reading = self.reading
        target = float(reading.percent or 0)
        target_bolt = 1.0 if reading.plugged else 0.0
        return (
            reading.charging
            or (reading.level == "critical" and not reading.plugged)
            or abs(self.shown_level - target) > 0.05
            or abs(self.bolt - target_bolt) > 0.01
            or self.flash > 0.01
        )

    def set_animating(self, on: bool) -> None:
        if on == self.animating:
            return
        self.animating = on
        if on:
            self.last_frame = time.monotonic()
            user32.SetTimer(self.hwnd, TIMER_FRAME, FRAME_MS, None)
        else:
            user32.KillTimer(self.hwnd, TIMER_FRAME)
            self.draw()

    def step(self) -> None:
        """Advance the eased values by the time since the last frame, then draw."""
        now = time.monotonic()
        dt = min(0.2, now - self.last_frame)
        self.last_frame = now
        reading = self.reading
        target = float(reading.percent or 0)
        self.shown_level += (target - self.shown_level) * (1 - math.exp(-dt / EASE_TAU))
        if abs(self.shown_level - target) < 0.05:
            self.shown_level = target
        target_bolt = 1.0 if reading.plugged else 0.0
        step = dt / BOLT_FADE
        self.bolt = min(target_bolt, self.bolt + step) if self.bolt < target_bolt else max(
            target_bolt, self.bolt - step)
        self.flash = max(0.0, self.flash - dt / FLASH_DECAY)
        self.draw()
        if not self.wants_animation():
            self.set_animating(False)

    # --------------------------------------------------------------- power

    def poll_power(self, why: str = "") -> None:
        before = self.reading
        reading = power.read()
        if reading == before and not why:
            return
        self.reading = reading
        if reading.plugged and not before.plugged:
            # The charger went in: sweep the fill up from empty and flash it once.
            self.shown_level = 0.0
            self.flash = 1.0
            self.anim_start = time.monotonic()
            log(f"charger in at {reading.percent}%")
        elif before.plugged and not reading.plugged:
            log(f"charger out at {reading.percent}%")
        if reading.charging:
            if not before.charging:
                self.rate.reset()
            if reading.percent is not None:
                self.rate.add(reading.percent)
        else:
            self.rate.reset()
        if self.wants_animation():
            self.set_animating(True)
        else:
            self.draw()

    # ----------------------------------------------------------------- ink

    def sample_ink(self) -> None:
        choice = self.config.get("ink", "auto")
        if choice != "auto":
            ink = choice
        else:
            left, top, width, height = self.rect()
            screen = user32.GetDC(None)
            margin = max(2, int(4 * self.scale))
            points = [(left + int(width * f), top - margin) for f in (0.1, 0.3, 0.5)]
            points += [(left + int(width * f), top + height + margin) for f in (0.1, 0.3, 0.5)]
            points += [(left - margin, top + height // 2)]
            samples = []
            for x, y in points:
                colour = gdi32.GetPixel(screen, x, y)
                if colour not in (0xFFFFFFFF, 0):
                    samples.append(luminance(colour))
            user32.ReleaseDC(None, screen)
            if not samples:
                return
            level = sum(samples) / len(samples)
            if level >= INK_DARK_ABOVE:
                ink = "dark"
            elif level <= INK_LIGHT_BELOW:
                ink = "light"
            else:
                return
        if ink != self.ink:
            self.ink = ink
            log(f"ink -> {ink}")
            self.draw()

    # ------------------------------------------------------------- desktop

    def pin_to_desktop(self) -> None:
        """Owned by Progman (visible through Show Desktop), at the bottom of the Z-order."""
        progman = user32.FindWindowW("Progman", None)
        changed = False
        if progman and user32.GetWindow(self.hwnd, GW_OWNER) != progman:
            # First run, or explorer restarted and made a new desktop window.
            user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, progman)
            log("owned by the desktop window")
            changed = True
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            changed = True
        if changed:
            self.apply_layer()

    def apply_layer(self) -> None:
        on_top = bool(self.config.get("on_top"))
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST if on_top else HWND_NOTOPMOST,
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if not on_top:
            user32.SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    # ---------------------------------------------------------------- menu

    def menu_items(self) -> list[tuple[int, str, bool]]:
        return [
            (MENU_SHOW_TIME, "Show time left", bool(self.config.get("show_time", True))),
            (MENU_ON_TOP, "Always on top", bool(self.config.get("on_top"))),
            (MENU_DRAGGABLE, "Draggable", bool(self.config.get("draggable", True))),
            (MENU_AUTOSTART, "Start with Windows", startup_shortcut().exists()),
        ]

    def submenus(self) -> list[tuple[str, list[tuple[int, str, bool]]]]:
        size = self.config.get("size")
        ink = self.config.get("ink", "auto")
        return [
            ("Size", [(MENU_SIZE_BASE + i, SIZES[k][0], k == size)
                      for i, k in enumerate(SIZE_KEYS)]),
            ("Colour", [(MENU_INK_BASE + i, INKS[k], k == ink) for i, k in enumerate(INK_KEYS)]),
        ]

    def open_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        for label, entries in self.submenus():
            child = user32.CreatePopupMenu()
            for ident, text, checked in entries:
                user32.AppendMenuW(child, MF_STRING | (MF_CHECKED if checked else 0), ident, text)
            user32.AppendMenuW(menu, MF_STRING | MF_POPUP, child or 0, label)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        for ident, text, checked in self.menu_items():
            user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if checked else 0), ident, text)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, MENU_FOLDER, "Open folder")
        user32.AppendMenuW(menu, MF_STRING, MENU_HIDE, "Hide to tray")
        user32.AppendMenuW(menu, MF_STRING, MENU_QUIT, "Quit")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        choice = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                       point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if choice:
            self.apply_choice(choice)

    def apply_choice(self, ident: int) -> None:
        if ident == MENU_SHOW_TIME:
            self.config["show_time"] = not bool(self.config.get("show_time", True))
            self.draw()
        elif ident == MENU_ON_TOP:
            self.config["on_top"] = not bool(self.config.get("on_top"))
            self.apply_layer()
        elif ident == MENU_DRAGGABLE:
            self.config["draggable"] = not bool(self.config.get("draggable", True))
        elif ident == MENU_AUTOSTART:
            set_autostart(not startup_shortcut().exists())
        elif MENU_SIZE_BASE <= ident < MENU_SIZE_BASE + len(SIZE_KEYS):
            key = SIZE_KEYS[ident - MENU_SIZE_BASE]
            self.config["size"], self.config["width"] = key, SIZES[key][1]
            self.apply_size()
        elif MENU_INK_BASE <= ident < MENU_INK_BASE + len(INK_KEYS):
            self.config["ink"] = INK_KEYS[ident - MENU_INK_BASE]
            self.sample_ink()
        elif ident == MENU_FOLDER:
            os.startfile(HERE)  # noqa: S606 - opening our own folder is the point
        elif ident == MENU_HIDE:
            user32.ShowWindow(self.hwnd, SW_HIDE)
        elif ident == MENU_QUIT:
            self.quit()
            return
        save_config(self.config)
        self.refresh_tray()

    def wheel_resize(self, delta: int) -> None:
        width = self.width_css() * (1.08 if delta > 0 else 1 / 1.08)
        self.config["width"] = int(round(max(MIN_CSS, min(MAX_CSS, width))))
        self.config["size"] = "custom"
        self.apply_size()
        save_config(self.config)

    # ------------------------------------------------------------- message

    def _handle_message(self, hwnd, message, wparam, lparam):  # noqa: C901 - a window proc
        try:
            if message == WM_TIMER:
                if wparam == TIMER_FRAME:
                    self.step()
                    return 0
                self._tick += 1
                if self._tick % POWER_TICKS == 0:
                    self.poll_power()
                if self._tick % INK_TICKS == 0:
                    self.sample_ink()
                if self._tick % DESKTOP_TICKS == 0:
                    self.pin_to_desktop()
                return 0
            if message == WM_POWERBROADCAST:
                self.poll_power("power event")
                return 1
            if message == WM_WINDOWPOSCHANGING and lparam and not self.config.get("on_top"):
                # A desktop widget never rises: every Z-order change becomes "bottom".  This is
                # what stops Win+D or a click on the wallpaper from dragging it over your apps.
                position = WINDOWPOS.from_address(lparam)
                if not position.flags & SWP_NOZORDER:
                    position.hwndInsertAfter = HWND_BOTTOM
                return 0
            if message == WM_SYSCOMMAND and (wparam & SC_MASK) == SC_MINIMIZE:
                return 0
            if message == WM_SIZE and wparam == SIZE_MINIMIZED:
                user32.ShowWindow(self.hwnd, SW_RESTORE)
                return 0
            if message == WM_NCHITTEST:
                picture = self.picture
                if picture is None:
                    return HTTRANSPARENT
                left, top, _, _ = self.rect()
                x = ctypes.c_short(lparam & 0xFFFF).value - left
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value - top
                if not picture.in_hit(x, y):
                    return HTTRANSPARENT
                return HTCAPTION if self.config.get("draggable", True) else HTCLIENT
            if message in (WM_RBUTTONUP, WM_NCRBUTTONUP):
                self.open_menu()
                return 0
            if message == WM_MOUSEWHEEL and wparam & MK_CONTROL:
                self.wheel_resize(ctypes.c_short((wparam >> 16) & 0xFFFF).value)
                return 0
            if message == WM_EXITSIZEMOVE:
                self.save_geometry()
                self.sample_ink()
                return 0
            if message in (WM_DPICHANGED, WM_DISPLAYCHANGE):
                self.apply_size()
                return 0
            if message == WM_APP_MENU:
                self.apply_choice(int(wparam))
                return 0
            if message == WM_APP_SHOW:
                user32.ShowWindow(self.hwnd, SW_SHOWNA)
                self.apply_layer()
                self.draw()
                return 0
            if message == WM_DESTROY:
                self.quit()
                return 0
        except Exception:  # a window procedure must never let an error escape
            log("window procedure error:\n" + traceback.format_exc())
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # ---------------------------------------------------------------- tray

    def tray_menu(self) -> pystray.Menu:
        def post(ident: int):
            # Tray callbacks run on pystray's thread; the window is only touched on its own.
            return lambda icon, item: user32.PostMessageW(self.hwnd, WM_APP_MENU, ident, 0)

        def item(ident, text, checked, radio=False):
            return pystray.MenuItem(text, post(ident), checked=lambda _i, on=checked: on,
                                    radio=radio)

        return pystray.Menu(
            *(pystray.MenuItem(label, pystray.Menu(*(item(i, t, c, True) for i, t, c in entries)))
              for label, entries in self.submenus()),
            pystray.Menu.SEPARATOR,
            *(item(i, t, c) for i, t, c in self.menu_items()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show", lambda icon, it: user32.PostMessageW(
                self.hwnd, WM_APP_SHOW, 0, 0), default=True),
            pystray.MenuItem("Open folder", post(MENU_FOLDER)),
            pystray.MenuItem("Quit", post(MENU_QUIT)),
        )

    def start_tray(self) -> None:
        self.icon = pystray.Icon("battery", tray_image(), WINDOW_TITLE, self.tray_menu())
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def refresh_tray(self) -> None:
        if self.icon is not None:
            try:
                self.icon.menu = self.tray_menu()
                self.icon.update_menu()
            except Exception:
                log("could not refresh the tray menu:\n" + traceback.format_exc())

    # ----------------------------------------------------------------- run

    def quit(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_TICK)
            user32.KillTimer(self.hwnd, TIMER_FRAME)
        user32.PostQuitMessage(0)

    def create_window(self) -> bool:
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASS()
        window_class.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        window_class.hInstance = instance
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        window_class.lpszClassName = CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            if ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                log(f"could not register the window class: {ctypes.get_last_error()}")
                return False
        style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self.config.get("on_top"):
            style |= WS_EX_TOPMOST
        progman = user32.FindWindowW("Progman", None)
        self.hwnd = user32.CreateWindowExW(style, CLASS_NAME, WINDOW_TITLE, WS_POPUP | WS_VISIBLE,
                                           0, 0, 1, 1, progman, None, instance, None) or 0
        if not self.hwnd:
            log(f"could not create the window: {ctypes.get_last_error()}")
            return False
        return True

    def run(self) -> int:
        if not self.create_window():
            return 1
        self.place()
        self.sample_ink()
        self.pin_to_desktop()
        self.apply_layer()
        self.poll_power("start")
        self.draw()
        user32.SetTimer(self.hwnd, TIMER_TICK, TICK_MS, None)
        self.start_tray()
        _, _, width, height = self.rect()
        log(f"widget up ({width}x{height} px, scale {self.scale:.2f}, {self.ink} ink, "
            f"{self.reading.percent}% {'charging' if self.reading.charging else 'on battery' if not self.reading.plugged else 'plugged in'})")
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        log("widget closed")
        return 0


# ---------------------------------------------------------------- reviews ---


def self_check() -> int:
    reading = power.read()
    print(f"reading     : {reading}")
    print(f"status line : {power.status_line(reading, None)}")
    width, height, _ = surface.size_for(275, 1.5)
    print(f"medium size : {width}x{height} px at 150%")
    return 0


def render_png(path: Path) -> int:
    """A sheet of the states over a dark and a light wallpaper, to look at without a window."""
    width, height, unit = surface.size_for(275, 1.5)
    states = [
        (78, "ok", "78%", "4h 17m left", False, None, 0.0),
        (54, "ok", "54%", "charging · full in 48m", True, 0.3, 1.0),
        (54, "ok", "54%", "charging · full in 48m", True, 0.75, 0.8),
        (22, "low", "22%", "1h 05m left", False, None, 0.0),
        (9, "critical", "9%", "low · 18m left", False, None, 0.0),
    ]
    sheet = Image.new("RGBA", (width * 2, height * len(states)))
    for row, (level, colour, text, status, charging, shimmer, bolt) in enumerate(states):
        for column, (ink, back) in enumerate((("light", (43, 58, 74)), ("dark", (214, 206, 190)))):
            frame = surface.Frame(width, height, unit, ink, level, colour, text, status,
                                  charging, shimmer, bolt)
            image, _ = surface.compose(frame)
            tile = Image.alpha_composite(Image.new("RGBA", (width, height), (*back, 255)), image)
            sheet.paste(tile, (column * width, row * height))
    sheet.save(path)
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Battery, a desktop widget")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--render", type=Path)
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    global DEBUG
    DEBUG = bool(args.debug)
    make_dpi_aware()
    sys.excepthook = lambda kind, value, tb: log(
        "unhandled error:\n" + "".join(traceback.format_exception(kind, value, tb)))
    if args.self_check:
        return self_check()
    if args.render:
        return render_png(args.render)
    if args.install_autostart:
        return 0 if set_autostart(True) else 1
    if user32.FindWindowW(CLASS_NAME, WINDOW_TITLE):
        print("Battery is already running.")
        return 0
    log("starting")
    try:
        return Widget().run()
    except Exception:
        log("widget failed:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())
