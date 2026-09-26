"""Unit + closed-loop simulation tests for slow4more. Stdlib only.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "slow4more", ROOT / "hooks" / "slow4more.py")
cc = importlib.util.module_from_spec(spec)
sys.modules["slow4more"] = cc
spec.loader.exec_module(cc)

_feeder_spec = importlib.util.spec_from_file_location(
    'slow4more_statusline', ROOT / 'hooks' / 'slow4more_statusline.py')
feeder = importlib.util.module_from_spec(_feeder_spec)
sys.modules['slow4more_statusline'] = feeder
_feeder_spec.loader.exec_module(feeder)

_inst_spec = importlib.util.spec_from_file_location(
    "install", ROOT / "install.py")
install = importlib.util.module_from_spec(_inst_spec)
sys.modules["install"] = install
_inst_spec.loader.exec_module(install)


def make_cfg(**over):
    cfg = json.loads(json.dumps(cc.DEFAULT_CONFIG))
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def fresh_state():
    return json.loads(json.dumps(cc.EMPTY_STATE))


class TestParser(unittest.TestCase):
    def test_statusline_rate_limits_shape(self):
        now = 1_000_000.0
        data = {"rate_limits": {
            "five_hour": {"used_percentage": 23.5,
                          "resets_at": now + 0.5 * cc.WINDOWS["five_hour"]},
            "seven_day": {"used_percentage": 41.2,
                          "resets_at": now + 0.25 * cc.WINDOWS["seven_day"]},
        }}
        snaps = {s.name: s for s in cc._parse_utilization_payload(data, now)}
        self.assertAlmostEqual(snaps["five_hour"].utilization, 0.235)
        self.assertAlmostEqual(snaps["five_hour"].elapsed_fraction, 0.5)
        self.assertAlmostEqual(snaps["seven_day"].utilization, 0.412)
        self.assertAlmostEqual(snaps["seven_day"].elapsed_fraction, 0.75)

    def test_small_percentage_not_misread_as_fraction(self):
        snaps = cc._parse_utilization_payload(
            {"five_hour": {"used_percentage": 1.2}}, 0.0)
        self.assertAlmostEqual(snaps[0].utilization, 0.012)

    def test_fraction_and_aliases(self):
        snaps = cc._parse_utilization_payload(
            {"5h": {"utilization": 0.62}, "week": {"used": 80}}, 0.0)
        by = {s.name: s for s in snaps}
        self.assertAlmostEqual(by["five_hour"].utilization, 0.62)
        self.assertAlmostEqual(by["seven_day"].utilization, 0.80)

    def test_iso_resets_at(self):
        import datetime
        now = datetime.datetime(2026, 7, 12, tzinfo=datetime.timezone.utc)
        resets = now + datetime.timedelta(hours=1)
        snaps = cc._parse_utilization_payload(
            {"five_hour": {"utilization": 0.5,
                           "resets_at": resets.isoformat()}},
            now.timestamp())
        self.assertAlmostEqual(snaps[0].elapsed_fraction, 0.8)  # 4h of 5h gone

    def test_missing_resets_defaults_mid_window(self):
        snaps = cc._parse_utilization_payload(
            {"five_hour": {"utilization": 0.9}}, 0.0)
        self.assertAlmostEqual(snaps[0].elapsed_fraction, 0.5)


class TestController(unittest.TestCase):
    def test_no_sleep_below_activation(self):
        cfg, st = make_cfg(), fresh_state()
        d = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.49, 0.01)], st, cfg, 1000.0)
        self.assertEqual(d.sleep, 0.0)

    def test_hard_limit_sleeps_full_cap(self):
        cfg, st = make_cfg(), fresh_state()
        cap = cfg["cache_ttl_seconds"] * cfg["sleep_cap_fraction"]
        d = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.98, 0.2)], st, cfg, 1000.0)
        self.assertAlmostEqual(d.sleep, cap)
        self.assertEqual(d.reason, "hard-limit")

    def test_cap_never_exceeded(self):
        cfg, st = make_cfg(), fresh_state()
        cap = cfg["cache_ttl_seconds"] * cfg["sleep_cap_fraction"]
        d = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.9, 0.1)], st, cfg, 1000.0)
        self.assertLessEqual(d.sleep, cap)

    def test_behind_pace_no_sleep(self):
        cfg, st = make_cfg(), fresh_state()
        d = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.6, 0.9)], st, cfg, 1000.0)
        self.assertEqual(d.sleep, 0.0)

    def test_neediest_window_wins(self):
        # compute_sleep paces every window it is handed; which windows reach it
        # is decided upstream by get_snapshots (see TestWindowSelection).
        cfg, st = make_cfg(), fresh_state()
        d = cc.compute_sleep([
            cc.WindowSnapshot("five_hour", 0.55, 0.50),
            cc.WindowSnapshot("seven_day", 0.80, 0.40),
        ], st, cfg, 1000.0)
        self.assertEqual(d.window, "seven_day")
        self.assertGreater(d.sleep, 0)

    def test_urgency_ramp_is_convex(self):
        a, b = 0.5, 0.97
        self.assertEqual(cc.urgency(0.49, a, b, 3.0), 0.0)
        self.assertEqual(cc.urgency(0.50, a, b, 3.0), 0.0)   # 0 at activation
        self.assertEqual(cc.urgency(0.97, a, b, 3.0), 1.0)   # 1 at hard limit
        self.assertEqual(cc.urgency(1.20, a, b, 3.0), 1.0)   # clamped above
        # Convex: the midpoint sits well below the linear 0.5.
        self.assertLess(cc.urgency((a + b) / 2, a, b, 3.0), 0.5)
        self.assertAlmostEqual(cc.urgency((a + b) / 2, a, b, 1.0), 0.5)
        # Monotone increasing in utilization.
        vals = [cc.urgency(u, a, b, 3.0) for u in (0.6, 0.7, 0.8, 0.9)]
        self.assertEqual(vals, sorted(vals))

    def test_migration_clears_legacy_integral(self):
        # A state.json carried over from the pre-urgency controller holds an
        # integral accumulated from the raw pace error. Left in place it makes
        # the ki term over-throttle at low utilization (the reported
        # regression); _migrate_state must zero it on upgrade.
        cfg = make_cfg(cache_ttl_seconds=3600)
        st = fresh_state()
        st["version"] = 1
        st["windows"]["five_hour"] = {
            "integral": 2000.0, "last_error": 0.1, "last_t": 1000.0}
        cc._migrate_state(st)
        self.assertEqual(st["version"], 2)
        self.assertEqual(st["windows"]["five_hour"]["integral"], 0.0)
        d = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.51, 0.27)], st, cfg, 1000.0)
        self.assertEqual(d.sleep, 0.0)

    def test_moderate_lead_low_util_ignored_but_high_util_throttles(self):
        # The reported regression: a 24-point pace lead at 51% utilization must
        # produce no sleep (plenty of headroom); the identical lead near the
        # limit must throttle hard.
        cfg = make_cfg(cache_ttl_seconds=3600)
        low = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.51, 0.27)], fresh_state(),
            cfg, 1000.0)
        self.assertEqual(low.sleep, 0.0)
        high = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.90, 0.66)], fresh_state(),
            cfg, 1000.0)
        self.assertGreater(high.sleep, 100.0)

    def test_cap_approach_ramps_to_cap_on_large_cache(self):
        # On a large cap (1h cache -> 2880s) the sleep must climb smoothly to
        # the cap near the limit instead of topping out far below it and jumping
        # only at the hard limit. The mid-range stays gentle, and the 5-minute
        # cap is unaffected (the PI term already dominates there).
        big = make_cfg(cache_ttl_seconds=3600)
        cap = big["cache_ttl_seconds"] * big["sleep_cap_fraction"]

        def sleep_at(cfg, u, e=0.30):
            return cc.compute_sleep(
                [cc.WindowSnapshot("five_hour", u, max(0.0, u - e))],
                fresh_state(), cfg, 1000.0).sleep

        mid = sleep_at(big, 0.85)
        near = sleep_at(big, 0.95)
        self.assertLess(mid, 0.2 * cap)       # gentle in the mid-range
        self.assertGreater(near, 0.4 * cap)   # climbs toward the cap near limit
        self.assertLess(near, cap)            # but not the full cap yet
        self.assertGreater(near, mid)
        # The approach term is a no-op on the 5-minute cap: same mid-range sleep
        # as the pure PI response, and cache-independent.
        self.assertAlmostEqual(
            sleep_at(make_cfg(cache_ttl_seconds=300), 0.85), mid, delta=0.5)


class TestSimulation(unittest.TestCase):
    """Closed-loop: a synthetic agent burns budget; the controller throttles."""

    def run_sim(self, budget, tokens_per_cycle, base_cycle_s, cfg,
                horizon_s=None):
        window = cc.WINDOWS["five_hour"]
        horizon = horizon_s or int(window * 3)
        st = fresh_state()
        t0, now, used = 0.0, 0.0, 0.0
        sleeps, exhausted_at = [], None
        while now - t0 < horizon:
            u = used / budget
            s = min(1.0, (now - t0) / window)
            snap = cc.WindowSnapshot("five_hour", u, s)
            d = cc.compute_sleep([snap], st, cfg, now)
            sleeps.append(d.sleep)
            now += base_cycle_s + d.sleep
            used += tokens_per_cycle
            if exhausted_at is None and used >= budget:
                exhausted_at = now - t0
                break
        return exhausted_at, sleeps

    def test_heavy_burn_stretched_past_window_on_1h_cache(self):
        # Unthrottled this workload exhausts a 5h budget in ~1h (5x pace). On
        # the subscription 1-hour cache (the installer default) the large sleep
        # cap lets the convex ramp stretch it comfortably past the window even
        # though throttling only ramps in near the top.
        cfg = make_cfg(cache_ttl_seconds=3600)
        window = cc.WINDOWS["five_hour"]
        budget = 1_000_000.0
        cycles_unthrottled = 3600 / 20.0
        w = budget / cycles_unthrottled
        exhausted_at, sleeps = self.run_sim(budget, w, 20.0, cfg)
        cap = cfg["cache_ttl_seconds"] * cfg["sleep_cap_fraction"]
        self.assertTrue(all(s <= cap + 1e-9 for s in sleeps))
        self.assertIsNotNone(exhausted_at)
        # Budget outlasts the whole window instead of dying at ~20% of it.
        self.assertGreater(exhausted_at, window,
                           f"exhausted after {exhausted_at/3600:.2f}h")
        # ...but the controller must not crawl forever either.
        self.assertLess(exhausted_at, 2.5 * window)

    def test_heavy_burn_5min_cache_delays_but_cap_limits_stretch(self):
        # With the 5-minute cache the cap is only 240s, so at the default
        # exponent a 5x burn cannot be stretched all the way across the window:
        # it is still delayed to several times its unthrottled 1h life, but
        # exhausts before the window closes. This pins the cap/exponent
        # tradeoff documented in the README (lower ramp_exponent or
        # activation_utilization if you are on the 5-minute cache).
        cfg = make_cfg(cache_ttl_seconds=300)
        budget = 1_000_000.0
        w = budget / (3600 / 20.0)
        exhausted_at, sleeps = self.run_sim(budget, w, 20.0, cfg)
        cap = cfg["cache_ttl_seconds"] * cfg["sleep_cap_fraction"]
        self.assertTrue(all(s <= cap + 1e-9 for s in sleeps))
        self.assertIsNotNone(exhausted_at)
        self.assertGreater(exhausted_at, 3 * 3600,   # >3h vs 1h unthrottled
                           f"exhausted after {exhausted_at/3600:.2f}h")
        # ...but the cap stops it short of the full window on the 5-min cache.
        self.assertLess(exhausted_at, cc.WINDOWS["five_hour"])

    def test_light_usage_never_throttles(self):
        cfg = make_cfg()
        window = cc.WINDOWS["five_hour"]
        budget = 1_000_000.0
        # Uses only 30% of budget across the whole window.
        cycles = window / 60.0
        w = 0.3 * budget / cycles
        _, sleeps = self.run_sim(budget, w, 60.0, cfg, horizon_s=window)
        self.assertEqual(sum(sleeps), 0.0)

    def test_no_throttle_before_activation(self):
        cfg = make_cfg()
        budget = 1_000_000.0
        st = fresh_state()
        now, used = 0.0, 0.0
        while used / budget < cfg["activation_utilization"] - 0.01:
            u = used / budget
            s = now / cc.WINDOWS["five_hour"]
            d = cc.compute_sleep(
                [cc.WindowSnapshot("five_hour", u, s)], st, cfg, now)
            self.assertEqual(d.sleep, 0.0, f"slept at u={u:.2f}")
            now += 20.0
            used += budget / 180.0

    def test_recovery_after_pause(self):
        """Behind pace after a long user pause -> throttle releases."""
        cfg, st = make_cfg(), fresh_state()
        # Drive it to heavy throttle first.
        d1 = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.8, 0.4)], st, cfg, 1000.0)
        self.assertGreater(d1.sleep, 0)
        # User pauses 1.5h; window keeps elapsing, utilization frozen.
        d2 = cc.compute_sleep(
            [cc.WindowSnapshot("five_hour", 0.8, 0.85)], st, cfg, 6400.0)
        self.assertEqual(d2.sleep, 0.0)


class TestIngest(unittest.TestCase):
    def _line(self, mid, in_t=100, out_t=10, cw=0, cr=0,
              ts="2026-07-12T10:00:00Z"):
        return json.dumps({
            "type": "assistant", "timestamp": ts,
            "message": {"id": mid, "usage": {
                "input_tokens": in_t, "output_tokens": out_t,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
            }},
        })

    def test_dedupe_and_weighting(self):
        cfg, st = make_cfg(), fresh_state()
        log = cc.Log(make_cfg(log_level="off"))
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as f:
            f.write(self._line("m1", 100, 10, 50, 1000) + "\n")
            f.write(self._line("m1", 100, 10, 50, 1000) + "\n")  # duplicate id
            f.write(json.dumps({"type": "user"}) + "\n")
            path = f.name
        cc.ingest_transcript(st, cfg, "sess1", path, 0.0, log)
        total = sum(st["events"].values())
        expected = 100 * 1.0 + 10 * 5.0 + 50 * 1.25 + 1000 * 0.1
        self.assertAlmostEqual(total, expected)
        os.unlink(path)

    def test_incremental_offset_and_partial_line(self):
        cfg, st = make_cfg(), fresh_state()
        log = cc.Log(make_cfg(log_level="off"))
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        full = self._line("m1") + "\n"
        with open(path, "w") as f:
            f.write(full[:30])  # partial line, no newline
        cc.ingest_transcript(st, cfg, "s", path, 0.0, log)
        self.assertEqual(sum(st["events"].values()), 0.0)
        with open(path, "a") as f:
            f.write(full[30:])
            f.write(self._line("m2") + "\n")
        cc.ingest_transcript(st, cfg, "s", path, 0.0, log)
        self.assertAlmostEqual(sum(st["events"].values()),
                               2 * (100 * 1.0 + 10 * 5.0))
        os.unlink(path)


class TestWindowSelection(unittest.TestCase):
    """get_snapshots enforces only the configured windows (default: 5h)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SLOW4MORE_STATE_DIR"] = self.tmp.name
        self.now = 2_000_000.0
        self.usage = os.path.join(self.tmp.name, "usage.json")
        with open(self.usage, "w") as f:
            json.dump({
                "five_hour": {"utilization": 0.55,
                              "resets_at": self.now + 0.5 * cc.WINDOWS["five_hour"]},
                "seven_day": {"utilization": 0.80,
                              "resets_at": self.now + 0.5 * cc.WINDOWS["seven_day"]},
            }, f)

    def tearDown(self):
        os.environ.pop("SLOW4MORE_STATE_DIR", None)
        self.tmp.cleanup()

    def _names(self, cfg):
        log = cc.Log(make_cfg(log_level="off"))
        snaps = cc.get_snapshots(fresh_state(), cfg, self.now, log)
        return sorted(s.name for s in snaps)

    def test_default_enforces_five_hour_only(self):
        # Both windows are present in the feed; only 5h survives by default,
        # even though 7d is the neediest.
        cfg = make_cfg(provider="file", usage_file=self.usage)
        self.assertEqual(self._names(cfg), ["five_hour"])

    def test_opt_in_seven_day(self):
        cfg = make_cfg(provider="file", usage_file=self.usage,
                       enforce_windows=["five_hour", "seven_day"])
        self.assertEqual(self._names(cfg), ["five_hour", "seven_day"])

    def test_empty_enforce_list_disables_all(self):
        cfg = make_cfg(provider="file", usage_file=self.usage,
                       enforce_windows=[])
        self.assertEqual(self._names(cfg), [])

    def test_null_enforce_falls_back_to_all_windows(self):
        # Distinct from the empty list: null means "no explicit selection", so
        # every known window is enforced.
        cfg = make_cfg(provider="file", usage_file=self.usage,
                       enforce_windows=None)
        self.assertEqual(self._names(cfg), ["five_hour", "seven_day"])


class TestUsageFileValidity(unittest.TestCase):
    """A usage.json window is trusted until its resets_at, never replaced by
    the transcript estimate while the file exists."""

    def setUp(self) -> None:
        self.tmp: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        os.environ['SLOW4MORE_STATE_DIR'] = self.tmp.name
        self.now: float = 2_000_000.0
        self.usage: str = os.path.join(self.tmp.name, 'usage.json')
        self.st: dict = fresh_state()
        self.st['events'][str(int(self.now // 60) * 60 - 60)] = 10_000_000.0

    def tearDown(self) -> None:
        os.environ.pop('SLOW4MORE_STATE_DIR', None)
        self.tmp.cleanup()

    def _write_usage(self, windows: dict, age: float) -> None:
        with open(self.usage, 'w') as f:
            json.dump(windows, f)
        mtime: float = self.now - age
        os.utime(self.usage, (mtime, mtime))

    def _snaps(self, provider: str = 'auto', log_level: str = 'off',
               now: float | None = None) -> list:
        cfg: dict = make_cfg(provider=provider, log_level=log_level)
        return cc.get_snapshots(self.st, cfg, now or self.now, cc.Log(cfg))

    def test_stale_file_trusted_until_resets_at(self) -> None:
        self._write_usage({'five_hour': {
            'used_percentage': 4, 'resets_at': self.now + 3600}}, age=3000)
        snaps: list = self._snaps()
        self.assertEqual([(s.name, s.source) for s in snaps],
                         [('five_hour', 'file')])
        self.assertAlmostEqual(snaps[0].utilization, 0.04)

    def test_expired_window_dropped_without_transcript_fallback(self) -> None:
        self._write_usage({'five_hour': {
            'used_percentage': 99, 'resets_at': self.now - 60}}, age=3000)
        snaps: list = self._snaps()
        self.assertEqual(snaps, [])
        self.assertEqual(
            cc.compute_sleep(snaps, self.st, make_cfg(), self.now).sleep, 0.0)

    def test_dropped_window_is_not_replaced_by_transcript(self) -> None:
        self._write_usage({'seven_day': {
            'used_percentage': 13,
            'resets_at': self.now + 86400}}, age=3000)
        self.assertEqual(self._snaps(), [])

    def test_file_provider_ignores_expired_hard_limit_snapshot(self) -> None:
        self._write_usage({'five_hour': {
            'used_percentage': 99, 'resets_at': self.now - 60}}, age=0)
        self.assertEqual(self._snaps(provider='file'), [])

    def test_stale_file_without_resets_at_falls_back_to_transcript(
            self) -> None:
        self._write_usage({'five_hour': {'used_percentage': 4}}, age=3000)
        snaps: list = self._snaps()
        self.assertEqual([(s.name, s.source) for s in snaps],
                         [('five_hour', 'transcript')])

    def test_missing_file_falls_back_to_transcript(self) -> None:
        snaps: list = self._snaps()
        self.assertEqual([s.source for s in snaps], ['transcript'])
        self.assertGreater(snaps[0].utilization, 1.0)

    def test_controller_resets_when_window_rolls_over(self) -> None:
        self.st['windows']['five_hour'] = {
            'integral': 3000.0, 'last_error': 0.2, 'last_t': self.now - 900,
            'resets_at': self.now - 10}
        new_reset: float = self.now + cc.WINDOWS['five_hour'] - 20
        self._write_usage({'five_hour': {
            'used_percentage': 1, 'resets_at': new_reset}}, age=0)
        self._snaps()
        wst: dict = self.st['windows']['five_hour']
        self.assertEqual(wst['integral'], 0.0)
        self.assertEqual(wst['last_t'], self.now)
        self.assertEqual(wst['resets_at'], new_reset)

    def test_controller_resets_for_resetless_data_after_rollover(
            self) -> None:
        self.st['windows']['five_hour'] = {
            'integral': 3000.0, 'last_error': 0.2, 'last_t': self.now - 900,
            'resets_at': self.now - 10}
        self._write_usage({'five_hour': {'used_percentage': 5}}, age=0)
        self._snaps()
        wst: dict = self.st['windows']['five_hour']
        self.assertEqual(wst['integral'], 0.0)
        self.assertEqual(wst['last_error'], 0.0)
        self.assertEqual(wst['last_t'], self.now)
        self.assertNotIn('resets_at', wst)

    def test_controller_kept_within_same_window(self) -> None:
        reset: float = self.now + 3600
        self.st['windows']['five_hour'] = {
            'integral': 3000.0, 'last_error': 0.2, 'last_t': self.now - 30,
            'resets_at': reset - 1}
        self._write_usage({'five_hour': {
            'used_percentage': 80, 'resets_at': reset}}, age=0)
        self._snaps()
        self.assertEqual(self.st['windows']['five_hour']['integral'], 3000.0)

    def test_expiry_logged_once_per_window(self) -> None:
        self._write_usage({'five_hour': {
            'used_percentage': 50, 'resets_at': self.now - 60}}, age=0)
        self._snaps(log_level='info')
        self._snaps(log_level='info', now=self.now + 30)
        with open(os.path.join(self.tmp.name, 'slow4more.log')) as f:
            lines: list = [ln for ln in f if 'expired' in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn('file window five_hour', lines[0])


class TestStatuslineFeeder(unittest.TestCase):
    """Sessions report only their own rate_limits; the feeder merges them so
    an idle session's stale view never overwrites a newer one."""

    now: float = 2_000_000.0

    def test_same_window_keeps_higher_percentage(self) -> None:
        reset: float = self.now + 3600
        merged: dict = feeder.merge_windows(
            {'seven_day': {'used_percentage': 14, 'resets_at': reset}},
            {'seven_day': {'used_percentage': 5, 'resets_at': reset}},
            self.now)
        self.assertEqual(merged['seven_day']['used_percentage'], 14)

    def test_window_missing_from_payload_is_kept(self) -> None:
        merged: dict = feeder.merge_windows(
            {'five_hour': {'used_percentage': 6, 'resets_at': self.now + 60}},
            {'seven_day': {'used_percentage': 5, 'resets_at': self.now + 9e4}},
            self.now)
        self.assertEqual(merged['five_hour']['used_percentage'], 6)
        self.assertEqual(merged['seven_day']['used_percentage'], 5)

    def test_new_window_replaces_expired_one(self) -> None:
        new: dict = {'used_percentage': 0,
                     'resets_at': self.now + cc.WINDOWS['five_hour']}
        merged: dict = feeder.merge_windows(
            {'five_hour': {'used_percentage': 90, 'resets_at': self.now - 10}},
            {'five_hour': new}, self.now)
        self.assertEqual(merged['five_hour'], new)

    def test_later_resets_at_wins(self) -> None:
        new: dict = {'used_percentage': 1,
                     'resets_at': self.now + cc.WINDOWS['five_hour']}
        merged: dict = feeder.merge_windows(
            {'five_hour': new},
            {'five_hour': {'used_percentage': 80, 'resets_at': self.now + 60}},
            self.now)
        self.assertEqual(merged['five_hour'], new)

    def test_expired_window_dropped_when_payload_lacks_it(self) -> None:
        merged: dict = feeder.merge_windows(
            {'five_hour': {'used_percentage': 50, 'resets_at': self.now - 5}},
            {'seven_day': {'used_percentage': 5, 'resets_at': self.now + 9e4}},
            self.now)
        self.assertNotIn('five_hour', merged)

    def test_existing_window_without_resets_at_is_not_carried_over(
            self) -> None:
        merged: dict = feeder.merge_windows(
            {'five_hour': {'used_percentage': 80}},
            {'seven_day': {'used_percentage': 5, 'resets_at': self.now + 9e4}},
            self.now)
        self.assertNotIn('five_hour', merged)
        self.assertIn('seven_day', merged)

    def test_main_merges_into_usage_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ['SLOW4MORE_STATE_DIR'] = tmp
            reset: float = 4_000_000_000.0
            with open(os.path.join(tmp, 'usage.json'), 'w') as f:
                json.dump({'five_hour': {'used_percentage': 6,
                                         'resets_at': reset}}, f)
            payload: dict = {'rate_limits': {'five_hour': {
                'used_percentage': 5, 'resets_at': reset}}}
            old_stdin, old_argv = sys.stdin, sys.argv
            sys.stdin, sys.argv = io.StringIO(json.dumps(payload)), ['feeder']
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(feeder.main(), 0)
            finally:
                sys.stdin, sys.argv = old_stdin, old_argv
                os.environ.pop('SLOW4MORE_STATE_DIR', None)
            with open(os.path.join(tmp, 'usage.json')) as f:
                written: dict = json.load(f)
        self.assertEqual(written['five_hour']['used_percentage'], 6)
        self.assertIn('written_at', written)


class TestHandleEvent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SLOW4MORE_STATE_DIR"] = self.tmp.name
        os.environ["SLOW4MORE_CONFIG"] = os.path.join(
            self.tmp.name, "config.json")
        usage = os.path.join(self.tmp.name, "usage.json")
        with open(usage, "w") as f:
            json.dump({"five_hour": {"utilization": 0.8,
                                     "resets_at": 2_000_000 + 10800}}, f)
        with open(os.environ["SLOW4MORE_CONFIG"], "w") as f:
            json.dump({"provider": "file", "usage_file": usage,
                       "log_level": "off"}, f)

    def tearDown(self):
        os.environ.pop("SLOW4MORE_STATE_DIR", None)
        os.environ.pop("SLOW4MORE_CONFIG", None)
        self.tmp.cleanup()

    def _event(self, hook="PreToolUse"):
        return {"hook_event_name": hook, "session_id": "sess-abc",
                "transcript_path": "", "tool_name": "Bash",
                "tool_input": {"command": "true"}}

    def test_sleep_and_dedupe(self):
        cfg = cc.load_config()
        log = cc.Log(cfg)
        slept = []
        out1 = cc.handle_event(self._event(), cfg, log,
                               sleep_fn=slept.append,
                               now_fn=lambda: 2_000_000.0)
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)
        self.assertIn("systemMessage", out1)  # big sleep -> user note
        # Second hook fires 1s later (parallel tool call) -> deduped.
        cc.handle_event(self._event(), cfg, log,
                        sleep_fn=slept.append,
                        now_fn=lambda: 2_000_000.0 + 1.0)
        self.assertEqual(len(slept), 1)

    def test_new_round_trip_sleeps_again(self):
        """Each API round-trip gets its own sleep, even in fast succession;
        same-turn calls (no new assistant message) stay deduped."""
        cfg = cc.load_config()
        log = cc.Log(cfg)
        transcript = os.path.join(self.tmp.name, "t.jsonl")

        def asst(mid):
            return json.dumps({"type": "assistant", "message": {
                "id": mid, "usage": {"input_tokens": 1, "output_tokens": 1},
            }}) + "\n"

        with open(transcript, "w") as f:
            f.write(asst("m1"))
        ev = dict(self._event(), transcript_path=transcript)
        slept = []
        t = [2_000_000.0]
        cc.handle_event(ev, cfg, log, sleep_fn=slept.append,
                        now_fn=lambda: t[0])
        self.assertEqual(len(slept), 1)
        # Same turn, 0.5s later, no new assistant message -> deduped.
        t[0] += slept[0] + 0.5
        cc.handle_event(ev, cfg, log, sleep_fn=slept.append,
                        now_fn=lambda: t[0])
        self.assertEqual(len(slept), 1)
        # New assistant message lands (new round-trip) -> sleeps again
        # immediately, no min_interval wait.
        with open(transcript, "a") as f:
            f.write(asst("m2"))
        t[0] += 0.2
        cc.handle_event(ev, cfg, log, sleep_fn=slept.append,
                        now_fn=lambda: t[0])
        self.assertEqual(len(slept), 2)

    def test_non_pretooluse_never_sleeps(self):
        cfg = cc.load_config()
        log = cc.Log(cfg)
        slept = []
        out = cc.handle_event(self._event("SessionStart"), cfg, log,
                              sleep_fn=slept.append,
                              now_fn=lambda: 2_000_000.0)
        self.assertEqual(slept, [])
        self.assertEqual(out, {})

    def test_fail_open_on_bad_usage_file(self):
        with open(os.path.join(self.tmp.name, "usage.json"), "w") as f:
            f.write("{corrupt")
        cfg = cc.load_config()
        log = cc.Log(cfg)
        slept = []
        out = cc.handle_event(self._event(), cfg, log,
                              sleep_fn=slept.append,
                              now_fn=lambda: 2_000_000.0)
        self.assertEqual(slept, [])
        self.assertEqual(out, {})


class TestInstaller(unittest.TestCase):
    """install.py writes the enforcement config and re-install honors flags."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "config.json")
        self.settings = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra):
        argv = ["install.py", "--apply", "--no-statusline",
                "--config", self.config, "--settings", self.settings, *extra]
        old = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = install.main()
        finally:
            sys.argv = old
        self.assertEqual(rc, 0)
        with open(self.config) as f:
            return json.load(f)

    def test_apply_defaults_to_five_hour_only(self):
        cfg = self._run()
        self.assertEqual(cfg["enforce_windows"], ["five_hour"])

    def test_enforce_seven_day_flag_writes_both(self):
        cfg = self._run("--enforce-seven-day")
        self.assertEqual(cfg["enforce_windows"], ["five_hour", "seven_day"])

    def _run_statusline(self, settings: dict, *extra: str) -> dict:
        with open(self.settings, 'w') as f:
            json.dump(settings, f)
        argv: list = ['install.py', '--apply', '--config', self.config,
                      '--settings', self.settings, *extra]
        old: list = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(install.main(), 0)
        finally:
            sys.argv = old
        with open(self.settings) as f:
            return json.load(f)['statusLine']

    def test_statusline_refresh_defaults_to_60(self) -> None:
        sl: dict = self._run_statusline(
            {'statusLine': {'type': 'command', 'command': 'my.sh',
                            'padding': 0}})
        self.assertEqual(sl['refreshInterval'], 60)
        self.assertEqual(sl['padding'], 0)
        self.assertIn('-- my.sh', sl['command'])

    def test_statusline_refresh_added_on_reinstall(self) -> None:
        sl: dict = self._run_statusline({'statusLine': {
            'type': 'command',
            'command': f'python3 {install.FEEDER} -- my.sh'}})
        self.assertEqual(sl['refreshInterval'], 60)

    def test_statusline_refresh_keeps_existing_value(self) -> None:
        sl: dict = self._run_statusline(
            {'statusLine': {'type': 'command', 'command': 'my.sh',
                            'refreshInterval': 5}},
            '--statusline-refresh', '30')
        self.assertEqual(sl['refreshInterval'], 5)

    def test_statusline_refresh_zero_leaves_it_unset(self) -> None:
        sl: dict = self._run_statusline({}, '--statusline-refresh', '0')
        self.assertNotIn('refreshInterval', sl)

    def test_reinstall_opts_into_seven_day(self):
        first = self._run()
        self.assertEqual(first["enforce_windows"], ["five_hour"])
        second = self._run("--enforce-seven-day")
        self.assertEqual(second["enforce_windows"],
                         ["five_hour", "seven_day"])


if __name__ == "__main__":
    unittest.main()
