#!/usr/bin/env python3
"""cc-slower: a Claude Code hook that adaptively slows sessions down to stay
inside the 5-hour / 7-day usage windows WITHOUT letting the prompt cache expire.

Design in one paragraph:
  On every PreToolUse event we estimate utilization of both usage windows,
  compare it against the "pace" (fraction of the window elapsed), and feed the
  overshoot into a PI(D) controller whose output is a sleep duration. The sleep
  is hard-capped below the prompt-cache TTL so a throttled session keeps its
  cache warm (a cold cache costs far more than it saves). State (controller
  integrals, window anchors, token accounting) is shared across sessions via a
  flock-protected JSON file, so several concurrent sessions throttle together.

Stdlib-only. Python >= 3.9. Never blocks or fails a tool call: every error
path logs and exits 0 (fail-open).
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WINDOWS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 24 * 3600,
}

DEFAULT_CONFIG = {
    # Which usage source to trust:
    #   "file"       - read utilization from usage_file (fed by the statusline
    #                  script, which receives official rate_limits data)
    #   "transcript" - count weighted tokens from local session transcripts
    #                  against the budgets below (works offline, needs calibration)
    #   "oauth"      - query an HTTP endpoint that reports window utilization
    #                  (undocumented API; explicit opt-in only)
    #   "auto"       - file if fresh, else transcript
    "provider": "auto",

    # Which usage windows to actually pace against, by canonical WINDOWS name.
    # Default: the 5-hour window only. The 7-day window rarely binds first and
    # pacing against it would slow every session all week for a limit that
    # resets weekly; opt in with ["five_hour", "seven_day"] if you want it.
    "enforce_windows": ["five_hour"],

    # Prompt-cache TTL. 300 for the default 5-minute cache, 3600 if your
    # requests use the 1-hour cache. The sleep cap derives from this.
    "cache_ttl_seconds": 300,
    # Sleep never exceeds cache_ttl * this fraction (leaves headroom for the
    # tool itself + model generation before the next cache read).
    "sleep_cap_fraction": 0.8,

    # No throttling at all below this utilization (user requirement: stay at
    # full speed until at least ~50% of a window is consumed).
    "activation_utilization": 0.50,
    # At/after this utilization, always sleep the full cap (limp-home mode).
    "hard_limit_utilization": 0.97,

    # Shape of the slowdown between activation and hard_limit. The controller's
    # authority is scaled by a convex "urgency" weight
    #   w(u) = ((u - activation) / (hard_limit - activation)) ** ramp_exponent
    # so a given pace lead earns almost no sleep far below the limit and close
    # to the full response as utilization approaches it. 1.0 = linear ramp from
    # activation; >1 stays flatter early and steepens near the top (3.0 keeps
    # sleeps negligible until ~75-80% even when well ahead of pace). This is
    # the primary "how aggressive" dial - raise it to slow later, lower it to
    # slow sooner.
    "ramp_exponent": 3.0,

    # Sleeps shorter than this are skipped (not worth the latency).
    "min_sleep_seconds": 2.0,
    # Within one session, at most one sleep per this many seconds. Parallel
    # tool calls in a single assistant turn share one API round-trip, so
    # sleeping in each of them would add latency without saving tokens.
    "min_interval_seconds": 5.0,

    # PI(D) gains. Error e = utilization - elapsed_fraction (dimensionless,
    # e.g. 0.10 = ten percentage points ahead of pace). Output is seconds.
    #   kp: seconds of sleep per unit of instantaneous pace error
    #   ki: seconds of sleep per unit of accumulated error (error * hours)
    #   kd: derivative gain, default 0 - utilization is a step-like, noisy
    #       signal and D mostly amplifies that noise.
    "kp": 900.0,
    "ki": 400.0,          # per error-hour
    "kd": 0.0,
    "integral_cap_hours": 1.0,   # anti-windup clamp on the integral term
    "integral_decay_tau": 900.0, # seconds; integral bleeds off when on-pace

    # Weighted-token cost model (transcript provider). Roughly proportional
    # to Anthropic pricing: cache reads are ~10x cheaper than input, output
    # ~5x input, cache writes 1.25x.
    "weights": {
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.1,
    },
    # Budgets in weighted tokens per window (transcript provider only).
    # THESE ARE PLACEHOLDERS - calibrate against /usage, see README.
    "budgets": {
        "five_hour": 4_000_000,
        "seven_day": 40_000_000,
    },

    # oauth provider settings. The token is read from token_env or token_file;
    # cc-slower never touches ~/.claude credentials on its own.
    "oauth": {
        "url": "https://api.anthropic.com/api/oauth/usage",
        "token_env": "CC_SLOWER_OAUTH_TOKEN",
        "token_file": None,
        "cache_seconds": 60,
        "timeout_seconds": 5,
    },

    # file provider: path to a JSON file, shape documented in README.
    # null = <state_dir>/usage.json (where the statusline feeder writes).
    "usage_file": None,
    # Ignore usage_file snapshots older than this (falls back to transcript).
    "usage_max_age_seconds": 600,

    # Show a user-visible note when a sleep exceeds this many seconds
    # (rate-limited to one note per notify_cooldown_seconds). null = never.
    "notify_threshold_seconds": 20,
    "notify_cooldown_seconds": 120,

    "log_level": "info",   # "debug" | "info" | "off"
}


def state_dir() -> Path:
    d = os.environ.get("CC_SLOWER_STATE_DIR")
    if d:
        return Path(d)
    xdg = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(xdg) / "cc-slower"


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = os.environ.get(
        "CC_SLOWER_CONFIG",
        os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
            "cc-slower", "config.json",
        ),
    )
    try:
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg


# ---------------------------------------------------------------------------
# Logging (append-only file in the state dir; never writes to stderr on the
# happy path so we don't pollute the user's session)
# ---------------------------------------------------------------------------

_LOG_LEVELS = {"debug": 10, "info": 20, "off": 100}


class Log:
    def __init__(self, cfg: dict):
        self.level = _LOG_LEVELS.get(cfg.get("log_level", "info"), 20)
        self.path = state_dir() / "cc-slower.log"

    def _write(self, lvl: str, msg: str) -> None:
        if _LOG_LEVELS[lvl] < self.level:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {lvl.upper()} {msg}\n")
        except OSError:
            pass

    def debug(self, msg: str) -> None:
        self._write("debug", msg)

    def info(self, msg: str) -> None:
        self._write("info", msg)


# ---------------------------------------------------------------------------
# State: one JSON file, flock-protected. The lock is held only for read/
# modify/write - never while sleeping - so concurrent hooks don't pile up.
# ---------------------------------------------------------------------------

EMPTY_STATE = {
    "version": 1,
    "sessions": {},        # sid -> {offset, transcript_path, last_sleep_end,
                           #         last_notify, seen_ids: [..], updated}
    "events": {},          # str(minute_epoch) -> weighted tokens
    "windows": {},         # name -> {start, integral, last_error, last_t}
    "oauth_cache": None,   # {fetched, data}
}


class StateFile:
    """Context manager: `with StateFile() as st: ... mutate st.data ...`"""

    def __init__(self):
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / "state.json"
        self.lock_path = d / "state.lock"
        self._lock_fd = None
        self.data = None

    def __enter__(self):
        self._lock_fd = open(self.lock_path, "a+")
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = json.loads(json.dumps(EMPTY_STATE))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                tmp = self.path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(self.data, f)
                os.replace(tmp, self.path)
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
        return False


# ---------------------------------------------------------------------------
# Usage providers. Each returns a list of WindowSnapshot.
# ---------------------------------------------------------------------------

@dataclass
class WindowSnapshot:
    name: str                 # "five_hour" | "seven_day"
    utilization: float        # 0..1+ fraction of the window budget consumed
    elapsed_fraction: float   # 0..1 fraction of the window's wall time elapsed


def _weighted(usage: dict, weights: dict) -> float:
    return (
        usage.get("input_tokens", 0) * weights["input"]
        + usage.get("output_tokens", 0) * weights["output"]
        + usage.get("cache_creation_input_tokens", 0) * weights["cache_write"]
        + usage.get("cache_read_input_tokens", 0) * weights["cache_read"]
    )


def ingest_transcript(st: dict, cfg: dict, session_id: str,
                      transcript_path: str, now: float, log: Log) -> None:
    """Incrementally parse new transcript lines into the per-minute event map.

    Offsets are tracked per session; assistant messages are deduped by message
    id because one API response can span several JSONL lines.
    """
    if not transcript_path:
        return
    sess = st["sessions"].setdefault(session_id, {})
    if sess.get("transcript_path") != transcript_path:
        sess["transcript_path"] = transcript_path
        sess["offset"] = 0
        sess["seen_ids"] = []
    offset = int(sess.get("offset", 0))
    seen = list(sess.get("seen_ids", []))
    seen_set = set(seen)
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return
    if size < offset:  # file rewritten (resume/compact) - re-scan, dedupe by id
        offset = 0
    added = 0.0
    try:
        with open(transcript_path, "rb") as f:
            f.seek(offset)
            for raw in f:
                offset += len(raw)
                if not raw.endswith(b"\n"):
                    offset -= len(raw)  # partial line; re-read next time
                    break
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if line.get("type") != "assistant":
                    continue
                msg = line.get("message") or {}
                mid = msg.get("id") or line.get("uuid")
                if mid in seen_set:
                    continue
                seen_set.add(mid)
                seen.append(mid)
                # Counts every new API response; the sleep dedupe uses this
                # to tell "new round-trip" from "same assistant turn".
                sess["asst_seen"] = int(sess.get("asst_seen", 0)) + 1
                usage = msg.get("usage")
                if not usage:
                    continue
                w = _weighted(usage, cfg["weights"])
                ts = now
                t = line.get("timestamp")
                if isinstance(t, str):
                    try:
                        import datetime
                        ts = datetime.datetime.fromisoformat(
                            t.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                minute = str(int(ts // 60) * 60)
                st["events"][minute] = st["events"].get(minute, 0.0) + w
                added += w
    except OSError:
        return
    sess["offset"] = offset
    sess["seen_ids"] = seen[-400:]
    sess["updated"] = now
    if added:
        log.debug(f"ingest session={session_id[:8]} +{added:.0f} weighted tokens")


def prune_state(st: dict, now: float) -> None:
    horizon = now - WINDOWS["seven_day"] - 3600
    st["events"] = {k: v for k, v in st["events"].items() if float(k) >= horizon}
    st["sessions"] = {
        sid: s for sid, s in st["sessions"].items()
        if now - s.get("updated", now) < 48 * 3600
    }


def transcript_snapshots(st: dict, cfg: dict, now: float) -> list:
    """Window utilization from locally-counted weighted tokens vs budgets.

    Window anchors mimic Anthropic's semantics: a window opens at the first
    activity after the previous one expired and lasts its full length.
    """
    snaps = []
    for name, length in WINDOWS.items():
        wst = st["windows"].setdefault(name, {})
        start = wst.get("start")
        if start is None or now - start >= length:
            # New window opens at the earliest activity within the last
            # `length` seconds (or now). Reset the controller for it.
            recent = [float(k) for k in st["events"] if float(k) > now - length]
            start = min(recent) if recent else now
            wst["start"] = start
            wst["integral"] = 0.0
            wst["last_error"] = 0.0
            wst["last_t"] = now
        used = sum(v for k, v in st["events"].items() if float(k) >= start)
        budget = float(cfg["budgets"][name])
        snaps.append(WindowSnapshot(
            name=name,
            utilization=used / budget if budget > 0 else 0.0,
            elapsed_fraction=min(1.0, (now - start) / length),
        ))
    return snaps


def _parse_utilization_payload(data: dict, now: float) -> list:
    """Tolerant parser for utilization JSON (oauth endpoint or usage_file).

    Accepts {"five_hour": {"utilization": 0.62, "resets_at": <epoch|iso>}, ...}
    and close variants. Keys whose name says "percentage"/"percent" are
    divided by 100; "utilization"/"used" are taken as fractions unless
    clearly a percentage (>1.5). Window keys like "5h"/"7d"/"week" are
    normalized. The Claude Code statusline `rate_limits` object (with
    used_percentage + resets_at) parses as-is.
    """
    alias = {
        "five_hour": "five_hour", "5h": "five_hour", "session": "five_hour",
        "seven_day": "seven_day", "7d": "seven_day", "week": "seven_day",
        "weekly": "seven_day",
    }
    if isinstance(data, dict) and isinstance(data.get("rate_limits"), dict):
        data = data["rate_limits"]
    snaps = []
    for key, val in (data or {}).items():
        name = alias.get(str(key).lower())
        if not name or not isinstance(val, dict):
            continue
        u = None
        for k in ("used_percentage", "percent", "used_pct"):
            if val.get(k) is not None:
                u = float(val[k]) / 100.0
                break
        if u is None:
            for k in ("utilization", "used_fraction", "used"):
                if val.get(k) is not None:
                    u = float(val[k])
                    if u > 1.5:  # clearly a percentage after all
                        u /= 100.0
                    break
        if u is None:
            continue
        length = WINDOWS[name]
        elapsed = None
        resets = val.get("resets_at", val.get("reset_at"))
        if resets is not None:
            try:
                if isinstance(resets, str):
                    import datetime
                    resets = datetime.datetime.fromisoformat(
                        resets.replace("Z", "+00:00")).timestamp()
                remaining = max(0.0, float(resets) - now)
                elapsed = min(1.0, max(0.0, 1.0 - remaining / length))
            except (ValueError, TypeError):
                elapsed = None
        if elapsed is None:
            # Without a reset time, assume we're mid-window; this makes the
            # controller act on absolute utilization only, conservatively.
            elapsed = 0.5
        snaps.append(WindowSnapshot(name, u, elapsed))
    return snaps


def oauth_snapshots(st: dict, cfg: dict, now: float, log: Log) -> list:
    o = cfg["oauth"]
    cache = st.get("oauth_cache")
    if cache and now - cache.get("fetched", 0) < o["cache_seconds"]:
        return _parse_utilization_payload(cache.get("data") or {}, now)
    token = os.environ.get(o["token_env"] or "", "")
    if not token and o.get("token_file"):
        try:
            token = Path(o["token_file"]).read_text().strip()
        except OSError:
            token = ""
    if not token:
        return []
    req = urllib.request.Request(o["url"], headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=o["timeout_seconds"]) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 - any network failure -> fall back
        log.info(f"oauth usage fetch failed: {e}")
        st["oauth_cache"] = {"fetched": now, "data": (cache or {}).get("data")}
        return _parse_utilization_payload((cache or {}).get("data") or {}, now)
    st["oauth_cache"] = {"fetched": now, "data": data}
    return _parse_utilization_payload(data, now)


def usage_file_path(cfg: dict) -> Path:
    return Path(cfg.get("usage_file") or (state_dir() / "usage.json"))


def file_snapshots(cfg: dict, now: float, log: Log, check_age: bool = True) -> list:
    path = usage_file_path(cfg)
    try:
        if check_age and now - path.stat().st_mtime > cfg["usage_max_age_seconds"]:
            log.debug(f"usage_file stale, ignoring: {path}")
            return []
        with open(path) as f:
            return _parse_utilization_payload(json.load(f), now)
    except (OSError, ValueError) as e:
        log.debug(f"usage_file unavailable ({e})")
        return []


def get_snapshots(st: dict, cfg: dict, now: float, log: Log) -> list:
    provider = cfg.get("provider", "auto")
    if provider == "file":
        snaps = file_snapshots(cfg, now, log, check_age=False)
    elif provider == "oauth":
        snaps = oauth_snapshots(st, cfg, now, log)
    elif provider == "auto":
        snaps = file_snapshots(cfg, now, log) or transcript_snapshots(st, cfg, now)
    else:
        snaps = transcript_snapshots(st, cfg, now)
    # Enforce only the configured windows (default: 5-hour only). Filtering
    # here keeps the 7-day window from being considered by any provider unless
    # the user explicitly opts in.
    enforce = cfg.get("enforce_windows")
    if enforce is None:
        enforce = list(WINDOWS)
    return [s for s in snaps if s.name in enforce]


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    sleep: float
    reason: str = ""
    window: str | None = None
    diag: dict = field(default_factory=dict)


def urgency(u: float, activation: float, hard_limit: float,
            exponent: float) -> float:
    """Convex ramp in [0, 1] that gates the controller by absolute utilization.

    Returns 0 at/below `activation`, 1 at/above `hard_limit`, and
    ((u - activation) / (hard_limit - activation)) ** exponent in between. With
    exponent > 1 the curve is flat early and steep near the limit, so a pace
    lead only turns into a real sleep as the window nears exhaustion.
    """
    if hard_limit <= activation:
        return 1.0 if u >= hard_limit else 0.0
    x = (u - activation) / (hard_limit - activation)
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return x ** exponent


def compute_sleep(snaps: list, st: dict, cfg: dict, now: float) -> Decision:
    """PI(D) pace controller, evaluated per window; the neediest window wins.

    The raw pace error is e = utilization - elapsed_fraction (e > 0 means we're
    burning budget faster than wall time is passing). We don't act on e
    directly: it is gated by a convex urgency weight w(u) (see urgency()) that
    is ~0 far below the limit and 1 near it, so the effective error the loop
    controls is ê = w(u) * max(e, 0). Consequences: the same 24-point pace lead
    is ignored at 51% utilization and near-maximal at 90%, and the integral -
    which accumulates ê - only winds up once you are genuinely close, so a
    short interactive burst that never lifts utilization cannot bank throttle.
    Output is clamped to [0, cache_ttl * sleep_cap_fraction] so the prompt cache
    never goes cold because of us.
    """
    cap = cfg["cache_ttl_seconds"] * cfg["sleep_cap_fraction"]
    integral_cap = cfg["integral_cap_hours"] * 3600.0
    activation = cfg["activation_utilization"]
    hard_limit = cfg["hard_limit_utilization"]
    exponent = float(cfg.get("ramp_exponent", 1.0))
    best = Decision(0.0)

    for snap in snaps:
        wst = st["windows"].setdefault(snap.name, {})
        last_t = wst.get("last_t", now)
        dt = min(max(now - last_t, 0.0), 600.0)  # stale gaps don't wind up
        e = snap.utilization - snap.elapsed_fraction
        w = urgency(snap.utilization, activation, hard_limit, exponent)
        e_eff = w * e if e > 0 else 0.0  # urgency-weighted, ahead-of-pace only

        integral = float(wst.get("integral", 0.0))
        if e_eff > 0:
            integral = min(integral + e_eff * dt, integral_cap)
        else:
            tau = float(cfg["integral_decay_tau"])
            integral *= math.exp(-dt / tau) if tau > 0 else 0.0

        deriv = 0.0
        if dt > 0 and cfg["kd"]:
            deriv = (e_eff - float(wst.get("last_error", e_eff))) / dt

        wst.update(integral=integral, last_error=e_eff, last_t=now)

        if snap.utilization >= hard_limit:
            out, why = cap, "hard-limit"
        elif e_eff <= 0:
            continue
        else:
            out = (cfg["kp"] * e_eff
                   + cfg["ki"] * (integral / 3600.0)
                   + cfg["kd"] * deriv)
            why = "pid"
        out = min(max(out, 0.0), cap)
        if out > best.sleep:
            best = Decision(out, why, snap.name, {
                "u": round(snap.utilization, 4),
                "elapsed": round(snap.elapsed_fraction, 4),
                "e": round(e, 4),
                "w": round(w, 4),
                "integral_h": round(integral / 3600.0, 4),
            })

    if best.sleep < cfg["min_sleep_seconds"]:
        return Decision(0.0, "below-min", best.window, best.diag)
    return best


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def handle_event(event: dict, cfg: dict, log: Log,
                 sleep_fn=time.sleep, now_fn=time.time) -> dict:
    """Process one hook event; returns the JSON object to print on stdout."""
    hook = event.get("hook_event_name", "")
    session_id = event.get("session_id", "unknown")
    transcript = event.get("transcript_path", "")
    now = now_fn()

    planned = Decision(0.0)
    notify = False
    with StateFile() as sf:
        st = sf.data
        prune_state(st, now)
        ingest_transcript(st, cfg, session_id, transcript, now, log)

        if hook not in ("PreToolUse",):
            return {}

        sess = st["sessions"].setdefault(session_id, {})
        # Dedupe: parallel/sequential tool calls in one assistant turn share
        # a single API round-trip - one sleep per round-trip is enough. A new
        # round-trip is recognized by a new assistant message having appeared
        # in the transcript since the last sleep; the min_interval grace
        # covers transcripts we fail to parse.
        last_end = float(sess.get("last_sleep_end", 0.0))
        new_response = int(sess.get("asst_seen", 0)) \
            > int(sess.get("slept_at_msgs", -1))
        if now < last_end:
            log.debug(f"skip (concurrent sleep) session={session_id[:8]}")
            return {}
        if not new_response and now < last_end + cfg["min_interval_seconds"]:
            log.debug(f"skip (same turn) session={session_id[:8]}")
            return {}

        snaps = get_snapshots(st, cfg, now, log)
        planned = compute_sleep(snaps, st, cfg, now)
        if planned.sleep > 0:
            # Record intent *before* sleeping (lock is released while we
            # sleep, so concurrent hooks see it and skip).
            sess["last_sleep_end"] = now + planned.sleep
            sess["slept_at_msgs"] = int(sess.get("asst_seen", 0))
            sess["updated"] = now
            thr = cfg.get("notify_threshold_seconds")
            if thr is not None and planned.sleep >= thr:
                if now - float(sess.get("last_notify", 0.0)) \
                        >= cfg["notify_cooldown_seconds"]:
                    sess["last_notify"] = now
                    notify = True

    if planned.sleep > 0:
        log.info(
            f"sleep {planned.sleep:.1f}s window={planned.window} "
            f"reason={planned.reason} {planned.diag} session={session_id[:8]}"
        )
        sleep_fn(planned.sleep)
        if notify:
            d = planned.diag
            return {"systemMessage": (
                f"cc-slower: throttling ~{planned.sleep:.0f}s/tool "
                f"({planned.window} window {d.get('u', 0) * 100:.0f}% used, "
                f"{d.get('elapsed', 0) * 100:.0f}% elapsed) to protect your "
                f"usage limit without dropping the prompt cache."
            )}
    return {}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    cfg = load_config()
    log = Log(cfg)
    try:
        out = handle_event(event, cfg, log)
        if out:
            print(json.dumps(out))
    except Exception as e:  # noqa: BLE001 - fail-open, never break the session
        log.info(f"ERROR {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
