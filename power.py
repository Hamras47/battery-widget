"""What the battery is doing, as plain data.

Windows' GetSystemPowerStatus gives the level, whether the charger is in and, on battery, an
estimate of the time left.  It never says how long charging will take, so that is estimated
here from how fast the level has been rising.
"""

from __future__ import annotations

import ctypes
import time
from collections import deque
from dataclasses import dataclass

#: BatteryFlag bits.
FLAG_CHARGING = 8
FLAG_NO_BATTERY = 128
FLAG_UNKNOWN = 255

#: Level thresholds for the fill colour.
LOW = 30
CRITICAL = 15


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_long),
        ("BatteryFullLifeTime", ctypes.c_long),
    ]


@dataclass(frozen=True)
class Reading:
    #: 0-100, or None when Windows does not know.
    percent: int | None
    plugged: bool
    charging: bool
    has_battery: bool
    #: Seconds of battery left, on battery only.
    seconds_left: int | None
    saver: bool

    @property
    def level(self) -> str:
        """"ok", "low" or "critical" -- what colour the fill is."""
        if self.percent is None or self.percent > LOW:
            return "ok"
        return "low" if self.percent > CRITICAL else "critical"

    @property
    def full(self) -> bool:
        return self.plugged and (self.percent or 0) >= 100


def read() -> Reading:
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return Reading(None, False, False, False, None, False)
    flag = status.BatteryFlag
    percent = None if status.BatteryLifePercent == 255 else min(100, status.BatteryLifePercent)
    has_battery = flag != FLAG_UNKNOWN and not flag & FLAG_NO_BATTERY
    plugged = status.ACLineStatus == 1
    life = status.BatteryLifeTime
    return Reading(
        percent=percent,
        plugged=plugged,
        charging=has_battery and plugged and bool(flag & FLAG_CHARGING),
        has_battery=has_battery,
        seconds_left=None if plugged or life < 0 or life == 0xFFFFFFFF else int(life),
        saver=bool(status.SystemStatusFlag & 1),
    )


class ChargeRate:
    """Time-to-full, from how fast the level has risen while charging.

    Needs a couple of percent of rise before it says anything: one step of 1% over a few
    seconds would promise a full battery in minutes.
    """

    WINDOW = 15 * 60
    MIN_RISE = 2

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def add(self, percent: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._samples and self._samples[-1][1] == percent:
            return
        self._samples.append((now, percent))
        while self._samples and now - self._samples[0][0] > self.WINDOW:
            self._samples.popleft()

    def seconds_to_full(self, percent: int, now: float | None = None) -> int | None:
        if len(self._samples) < 2:
            return None
        now = time.monotonic() if now is None else now
        first_time, first_percent = self._samples[0]
        rise = percent - first_percent
        if rise < self.MIN_RISE or now <= first_time:
            return None
        per_second = rise / (now - first_time)
        return int((100 - percent) / per_second)


def duration(seconds: int | None) -> str:
    """3h 12m, 48m, <1m."""
    if seconds is None:
        return ""
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def status_line(reading: Reading, to_full: int | None, show_time: bool = True) -> str:
    """The small line under the percentage."""
    if not reading.has_battery:
        return "no battery"
    if reading.full:
        return "fully charged"
    if reading.charging:
        if show_time and to_full:
            return f"charging · full in {duration(to_full)}"
        return "charging"
    if reading.plugged:
        return "plugged in"
    parts = []
    if reading.level == "critical":
        parts.append("low")
    elif reading.saver:
        parts.append("saver")
    if show_time and reading.seconds_left:
        parts.append(f"{duration(reading.seconds_left)} left")
    return " · ".join(parts) or "on battery"
