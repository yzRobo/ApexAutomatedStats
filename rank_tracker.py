"""
Apex Legends Status (ALS) API rank tracker.

Background-polls the ALS bridge endpoint for each player's RP, caching
current and previous values in a thread-safe dict. Designed to run as a daemon
thread alongside the main OCR watcher so rank/RP data can be spliced into match
logs once the EA cache catches up (2-3 min delay after a match ends).

See ALS_RP_TRACKER_SPEC.md for the full design rationale.
"""

import json
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime


_ALS_BRIDGE = "https://api.apexlegendsstatus.com/bridge"


class RankTracker:
    """Thread-safe ALS RP poller.

    Parameters
    ----------
    api_key : str
        ALS API key (sent as the ``Authorization`` header).
    known_names : list[str]
        Player names to poll (from config.json ``known_names``).
    poll_seconds : int | float
        Seconds between full polling cycles (default 120, per spec).
    platform : str
        Platform string for the ALS bridge query (default ``"PC"``).
    uids : dict[str, str] | None
        Optional ``{name: uid}`` overrides for accounts ALS can't resolve by
        name (e.g. Steam accounts → query by SteamID64). From config.json
        ``als_uids``.
    on_poll : callable | None
        Optional callback invoked after each full poll cycle with a snapshot
        ``{name: {current_rp, previous_rp, rank_name, rank_div, ...}}`` (same
        shape as :meth:`get_all`). Used to persist the live rank snapshot (e.g.
        upsert to Supabase) so the dashboard can show current rank/RP without a
        match. Exceptions in the callback are swallowed so polling never breaks.
    """

    def __init__(self, api_key, known_names, poll_seconds=120, platform="PC",
                 uids=None, on_poll=None):
        self._api_key = api_key
        self._names = list(known_names)
        self._poll_seconds = max(10, poll_seconds)
        self._platform = platform
        # Optional {name: uid} overrides. ALS can't always map an Apex display
        # name to a UID (notably Steam accounts), so those are looked up by UID
        # (e.g. a SteamID64) instead of by name. Keys/values coerced to str.
        self._uids = {str(k): str(v) for k, v in (uids or {}).items() if v}
        self._on_poll = on_poll

        # {name: {"current_rp": int|None, "previous_rp": int|None,
        #          "rank_name": str, "rank_div": int,
        #          "last_updated": float}}
        self._cache = {}
        self._lock = threading.Lock()
        # Serializes whole poll cycles so the background loop and an on-demand
        # force_poll_now() can't hammer the API concurrently and blow the
        # 5 req/s rate limit (each cycle already paces itself at 1 req/s).
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Public API (thread-safe)
    # ------------------------------------------------------------------ #
    def get_rp(self, name):
        """Return ``(current_rp, previous_rp)`` for *name*, or ``(None, None)``."""
        with self._lock:
            entry = self._cache.get(name)
            if entry is None:
                return None, None
            return entry["current_rp"], entry["previous_rp"]

    def get_all(self):
        """Return a snapshot ``{name: {current_rp, previous_rp, ...}}``."""
        with self._lock:
            return {k: dict(v) for k, v in self._cache.items()}

    def force_poll_now(self):
        """Trigger an immediate poll cycle (runs on THIS thread).

        Useful for the accelerated-polling flow: call this right after a match
        ends, then again 60s / 120s later to catch the EA cache update early.
        """
        self._poll_all()

    def force_poll_names(self, names):
        """Immediately refresh only *names* (runs on THIS thread).

        Targeted version of :meth:`force_poll_now` for the RP resolver, which
        only ever cares about the handful of players whose RP is still pending.
        Polling just those (instead of the whole roster) keeps the lock hold and
        the ALS call count proportional to the work left, not the roster size.
        Held under ``_poll_lock`` so it can't overlap the background cycle.
        """
        wanted = [n for n in dict.fromkeys(names) if n]  # de-dup, keep order
        if not wanted:
            return
        with self._poll_lock:
            for name in wanted:
                if self._stop.is_set():
                    break
                self._fetch_one(name)
                if not self._stop.is_set():
                    time.sleep(1.0)  # 5 req/s safety (spec §2.4)

    def stop(self):
        """Signal the background thread to stop (does NOT block)."""
        self._stop.set()

    # ------------------------------------------------------------------ #
    # Background loop
    # ------------------------------------------------------------------ #
    def _loop(self):
        """Daemon loop: poll all players, sleep, repeat."""
        # Initial poll immediately on start.
        self._poll_all()
        while not self._stop.is_set():
            self._stop.wait(self._poll_seconds)
            if self._stop.is_set():
                break
            self._poll_all()

    def _poll_all(self):
        """Fetch RP for every player, sleeping 1 s between requests.

        Held under ``_poll_lock`` so the background loop and any on-demand
        ``force_poll_now()`` run one at a time rather than overlapping.
        """
        with self._poll_lock:
            for name in self._names:
                if self._stop.is_set():
                    break
                self._fetch_one(name)
                # Rate-limit: 1 s between individual player requests (spec §2.4).
                if not self._stop.is_set():
                    time.sleep(1.0)
        # Persist the fresh snapshot (e.g. to Supabase) outside the poll lock.
        # Best-effort: a callback failure must never stop the polling loop.
        if self._on_poll is not None:
            try:
                self._on_poll(self.get_all())
            except Exception as exc:
                _log(f"[RankTracker] on_poll callback error – {exc}")

    def _fetch_one(self, name):
        """Query the ALS bridge for *name* and update the cache.

        Looks up by ``uid`` when one is configured for *name* (more reliable;
        required for Steam accounts), otherwise by ``player`` name.
        """
        uid = self._uids.get(name)
        if uid:
            url = (f"{_ALS_BRIDGE}?uid={urllib.parse.quote(uid)}"
                   f"&platform={self._platform}")
        else:
            url = (f"{_ALS_BRIDGE}?player={urllib.parse.quote(name)}"
                   f"&platform={self._platform}")
        req = urllib.request.Request(url, headers={
            "Authorization": self._api_key,
            "User-Agent": "apex-tracker",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            rp = _extract_rp(data)
            rank_name = _extract_rank_name(data)
            rank_div = _extract_rank_div(data)
            with self._lock:
                prev = self._cache.get(name, {}).get("current_rp")
                self._cache[name] = {
                    "current_rp": rp,
                    "previous_rp": prev,
                    "rank_name": rank_name,
                    "rank_div": rank_div,
                    "last_updated": time.time(),
                }
            _log(f"[RankTracker] {name}: RP={rp}  rank={rank_name} div={rank_div}")
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 400:
                _log(f"[RankTracker] {name}: 400 – EA API issue, will retry next cycle")
            elif code == 403:
                _log(f"[RankTracker] {name}: 403 – bad API key or unauthorized")
            elif code == 404:
                _log(f"[RankTracker] {name}: 404 – player not found on {self._platform}")
            elif code == 429:
                _log(f"[RankTracker] {name}: 429 – rate limit; backing off")
                time.sleep(5.0)  # extra back-off
            else:
                _log(f"[RankTracker] {name}: HTTP {code}")
        except Exception as exc:
            _log(f"[RankTracker] {name}: error – {exc}")


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _extract_rp(data):
    """``global.rank.rankScore`` → int, or None."""
    try:
        return int(data["global"]["rank"]["rankScore"])
    except (KeyError, TypeError, ValueError):
        return None


def _extract_rank_name(data):
    """``global.rank.rankName`` → str, or ''."""
    try:
        return data["global"]["rank"]["rankName"]
    except (KeyError, TypeError):
        return ""


def _extract_rank_div(data):
    """``global.rank.rankDiv`` → int, or 0."""
    try:
        return int(data["global"]["rank"]["rankDiv"])
    except (KeyError, TypeError, ValueError):
        return 0


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")
