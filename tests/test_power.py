import unittest

import power
import surface
from power import Reading


def reading(**kw):
    base = dict(percent=50, plugged=False, charging=False, has_battery=True,
                seconds_left=None, saver=False)
    base.update(kw)
    return Reading(**base)


class StatusLine(unittest.TestCase):
    def test_on_battery_with_time(self):
        self.assertEqual(power.status_line(reading(seconds_left=3 * 3600 + 720), None), "3h 12m left")

    def test_charging_with_estimate(self):
        r = reading(plugged=True, charging=True)
        self.assertEqual(power.status_line(r, 48 * 60), "charging · full in 48m")
        self.assertEqual(power.status_line(r, None), "charging")

    def test_full_and_plugged(self):
        self.assertEqual(power.status_line(reading(percent=100, plugged=True), None), "fully charged")
        self.assertEqual(power.status_line(reading(percent=80, plugged=True), None), "plugged in")

    def test_critical(self):
        r = reading(percent=9, seconds_left=18 * 60)
        self.assertEqual(r.level, "critical")
        self.assertEqual(power.status_line(r, None), "low · 18m left")


class Rate(unittest.TestCase):
    def test_needs_a_real_rise(self):
        rate = power.ChargeRate()
        rate.add(50, now=0)
        rate.add(51, now=60)
        self.assertIsNone(rate.seconds_to_full(51, now=60))

    def test_estimate(self):
        rate = power.ChargeRate()
        rate.add(50, now=0)
        rate.add(52, now=120)  # 1% a minute
        self.assertEqual(rate.seconds_to_full(52, now=120), 48 * 60)


class Render(unittest.TestCase):
    def test_background_is_untouched(self):
        w, h, u = surface.size_for(275, 1.5)
        frame = surface.Frame(w, h, u, "light", 54, "ok", "54%", "charging", True, 0.5, 1.0)
        picture = surface.render(frame)
        self.assertEqual(len(picture.pixels), w * h * 4)
        # The far corners are outside the pill and the text: nothing painted, clicks pass through.
        self.assertEqual(picture.pixels[3], 0)
        self.assertEqual(picture.pixels[-1], 0)


if __name__ == "__main__":
    unittest.main()
