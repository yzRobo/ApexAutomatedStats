"""
Apex Legends Status (ALS) API rank tracker.

Background-polls the ALS bridge endpoint for each player's RP, caching
current and previous values in a thread-safe dict. Designed to run as a daemon
thread alongside the main OCR watcher so rank/RP data can be spliced into match
logs once the EA cache catches up (2-3 min delay after a match ends).

See ALS_RP_TRACKER_SPEC.md for the full design rationale.
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from collections import deque
from datetime import datetime


_ALS_BRIDGE = "https://api.apexlegendsstatus.com/bridge"
# Map rotation: the current and next map, with start/end times, for each playlist.
_ALS_ROTATION = "https://api.apexlegendsstatus.com/maprotation?version=2"
# The playlists whose map is stored per match: pubs (ALS "battle_royale") and ranked.
# The match row stores both; the dashboard picks one with the match's ranked flag.
ROTATION_MODES = ("battle_royale", "ranked")
# Refetch the rotation when the stored windows stop covering the next 10 minutes, and
# anyway every 30 min so a schedule ALS changes mid-window is picked up.
_ROTATION_AHEAD = 10 * 60
_ROTATION_REFRESH = 30 * 60
# The least time between two fetches, whether the last one worked or not.
_ROTATION_RETRY = 5 * 60
# Windows that ended longer ago than this are dropped from memory and disk.
_ROTATION_KEEP = 2 * 86400
# Without a match start time, the map is still known when the same map was up for
# this whole span before the summary was read (a BR match is ~25 min at most).
_MATCH_MAX_SECS = 30 * 60

# Per-player in-memory ledger ring buffer size. At the 30s default cadence this
# holds ~16h of readings, plenty for a session delta and the resolver's windows;
# the durable history lives in the on-disk ledger regardless.
_LEDGER_RING = 2000
# Rotate the ledger file once it grows past this on startup (keeps one backup).
_LEDGER_MAX_BYTES = 10 * 1024 * 1024
# Per-player presence ring (state / legend readings). Small: only the last match
# or two matter for legend attribution.
_PRESENCE_RING = 400
# A legend reading counts for a match only if it was taken in the last 45 min and
# its in-match state began no earlier than that (a BR match is ~25 min at most).
_MATCH_WINDOW = 45 * 60
# Squadmates enter a match within seconds of each other; readings whose state-since
# times are further apart than this belong to different matches.
_SINCE_TOLERANCE = 120

# Serializes ALL ledger FILE I/O across RankTracker instances. A reconfigure
# (Stop->Start with changed roster/key) builds a new tracker on the SAME ledger
# path while the old daemon may still fire one final append; a module-wide lock
# (not the per-instance _ledger_lock) keeps that append from racing the new
# instance's startup rotate/hydrate of the identical file.
_LEDGER_FILE_LOCK = threading.Lock()

# Rank tiers low→high, for detecting a season reset (a large RP drop *alongside* a
# tier drop). Matched by lowercase prefix against ALS ``rankName``.
_TIER_ORDER = ["rookie", "bronze", "silver", "gold", "platinum",
               "diamond", "master", "apex predator"]


def _tier_index(tier):
    """Index of *tier* in :data:`_TIER_ORDER` (higher = better), or -1 if unknown."""
    if not tier:
        return -1
    t = str(tier).strip().lower()
    for i, name in enumerate(_TIER_ORDER):
        if t.startswith(name):
            return i
    return -1


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
    ledger_path : str | None
        Path to the durable JSONL RP ledger (``rp_ledger.jsonl``, next to
        ``rp_pending.json``). Every successful reading is appended here and mirrored
        into an in-memory ring buffer; :meth:`get_observations` and
        :meth:`get_session_delta` read it. None disables on-disk persistence (the
        ring buffer still works) — used by unit tests.
    retention_days : int
        On startup, rotate the ledger to ``rp_ledger.1.jsonl`` if it is older than
        this or exceeds ~10 MB (one generation kept).
    reset_drop : int
        An RP drop larger than this in one step, alongside a tier drop, marks a
        season-reset discontinuity (``"reset": true``) that consumers must not
        compute deltas across. A ``rankedSeason`` change also marks a reset.
    rotation_path : str | None
        Where to keep the map-rotation windows seen so far (``map_rotation.json``).
        Setting it turns on the map-rotation fetch (one ALS call when the stored
        windows run out, or every 30 min); None leaves it off, as in unit tests.
    """

    def __init__(self, api_key, known_names, poll_seconds=30, platform="PC",
                 uids=None, on_poll=None, ledger_path=None,
                 retention_days=14, reset_drop=800, rotation_path=None):
        self._api_key = api_key
        self._names = list(known_names)
        self._poll_seconds = max(10, poll_seconds)
        self._platform = platform
        # Optional {name: uid} overrides. ALS can't always map an Apex display
        # name to a UID (notably Steam accounts), so those are looked up by UID
        # (e.g. a SteamID64) instead of by name. Keys/values coerced to str.
        self._uids = {str(k): str(v) for k, v in (uids or {}).items() if v}
        self._on_poll = on_poll

        # --- Always-on RP ledger (RP_TRACKING_V2_SPEC.md §4.1) --------------- #
        # Every successful reading is appended to a durable JSONL ledger AND to a
        # per-player in-memory ring buffer. Session RP (latest − session baseline)
        # and the resolver's per-match classification both read from this ledger;
        # it is the self-healing, TAS-parity source of truth. Absent ledger_path
        # (e.g. a unit test) disables persistence but keeps the ring buffer.
        self._ledger_path = ledger_path
        self._reset_drop = reset_drop
        self._ledger_lock = threading.Lock()
        # {name: deque[{"ts", "rp", "reset"}]} — readers hold _ledger_lock.
        self._obs = {}
        # Presence for legend attribution: {name: deque[{"ts", "state", "since",
        # "legend"}]} from every successful response, including ones with no usable
        # RP (unranked players, degraded rankScore). Seeded from the ledger so a
        # restart mid-match keeps its readings. Readers hold _lock (created below,
        # before the poll thread starts; hydration runs single-threaded).
        self._presence = {}
        # Per-name last-seen season / rp / tier for reset detection across reads
        # (and across restarts, seeded from the hydrated ledger).
        self._last_season = {}
        self._last_rp = {}
        self._last_tier = {}
        # Session = one watch run; the baseline is the first reading after this.
        self._session_start = time.time()
        if self._ledger_path:
            self._rotate_ledger(retention_days)
            self._hydrate_ledger()

        # Self-healing roster tracking:
        #  - _ever_resolved: names that resolved on ALS at least once, so an
        #    empty cache entry means "can't find this player", not "not polled".
        #  - _notfound_counts: consecutive 404s per name (drives the warning).
        #  - _warned: names already warned about, so we don't spam.
        #  - _learned_uids: {name: uid} auto-discovered this session from a
        #    successful name lookup (used to pin future polls to the UID).
        self._ever_resolved = set()
        self._notfound_counts = {}
        self._warned = set()
        self._learned_uids = {}
        # {roster name: current Apex name per ALS}. Polling by UID keeps working
        # after a rename, so this is how a renamed player's new summary name is
        # mapped back to the roster (see name_aliases()).
        self._als_names = {}
        # Health for status displays (see health()): when the last usable reading
        # arrived and the last failure seen. Every failure used to go only to
        # print(), which the GUI build discards, so a rejected key or an ALS outage
        # left RP silently off for days while match stats kept logging.
        self._started_ts = time.time()
        self._last_ok_ts = None       # last reading with a real rankScore
        self._last_contact_ts = None  # last HTTP 200 of any kind
        self._last_error = None

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

        # Map rotation windows: {mode: {start: {"start", "end", "map"}}}. ALS only
        # gives the current and next window, so every window seen is kept (and saved
        # to rotation_path) to answer "which map was up when this match began" after
        # the rotation has moved on. Readers hold _rotation_lock.
        self._rotation_path = rotation_path
        self._rotation = {m: {} for m in ROTATION_MODES}
        self._rotation_lock = threading.Lock()
        self._rotation_fetched = 0.0   # last successful fetch
        self._rotation_retry_at = 0.0  # no fetch before this (after a failure)
        if self._rotation_path:
            self._load_rotation()

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

    def health(self):
        """``(state, detail)`` for status displays: ``'starting'`` until the first
        cycle has had time to finish, ``'ok'`` while usable readings keep arriving,
        ``'failing'`` (with the last error) once none has for max(3 poll cycles,
        120 s). A player whose rankScore is always 0 doesn't count against it as
        long as other players still read."""
        window = max(3 * self._poll_seconds, 120)
        now = time.time()
        with self._lock:
            last_ok, contact, err = self._last_ok_ts, self._last_contact_ts, self._last_error
        if last_ok is not None and now - last_ok < window:
            return "ok", None
        if contact is not None and now - contact < window:
            # ALS answers but nobody has ranked RP (off-season / all unranked):
            # nothing is broken, there's just no RP to track.
            return "ok", "ALS reachable; no ranked RP to track right now"
        if last_ok is None and now - self._started_ts < window:
            return "starting", None
        return "failing", err or "no readings from ALS"

    def unresolved_names(self):
        """Roster names that have NEVER resolved on ALS (misspelled, dropped,
        or wrong platform). These players still log match stats via OCR but get
        no RP; the watcher surfaces this so they aren't silently untracked."""
        with self._lock:
            return sorted(n for n in self._names
                          if n not in self._ever_resolved
                          and self._notfound_counts.get(n, 0) >= 2)

    def learned_uids(self):
        """``{name: uid}`` auto-discovered from successful name lookups this
        session (so the caller can offer to persist them to config)."""
        with self._lock:
            return dict(self._learned_uids)

    def name_aliases(self):
        """``{current Apex name: roster name}`` for players whose name on ALS no
        longer matches their roster entry (a rename; polls by UID keep working)."""
        with self._lock:
            return {als: roster for roster, als in self._als_names.items()
                    if als and als.lower() != roster.lower()}

    # ------------------------------------------------------------------ #
    # Presence — which legend each player used in a match
    # ------------------------------------------------------------------ #
    def _last_in_match(self, name, at):
        """Latest ``inMatch`` reading for *name* in the window ending at *at*, or
        None. Caller holds ``_lock``."""
        for o in reversed(self._presence.get(name) or ()):
            if o["ts"] > at:
                continue
            if o["ts"] < at - _MATCH_WINDOW:
                return None
            if o["state"] != "inMatch":
                continue
            if o["since"] is not None and o["since"] < at - _MATCH_WINDOW:
                return None  # a stuck/stale state, not a match that just ended
            return o
        return None

    def match_legends(self, names, at=None, anchor=None, after=None):
        """``{name: legend}`` for the match that just ended at *at* (default now).

        Each player's legend is the one ALS reported in their latest ``inMatch``
        reading (the last reading is past legend select). Readings from another
        match are left out:
          - *after*: when the previous match was logged. This match started after
            it, so a reading whose match began well before it (a short game that no
            poll landed in) belongs to the game before.
          - the match's state-since time. Squadmates enter a match within seconds of
            each other. *anchor* (this PC's own player, who is on the summary so is
            in this match) sets it when they have a reading; otherwise the time most
            players share does, and a tie sets nothing - with two players, one may
            be a teammate who quit and already queued into another game.
        Players with no usable reading (not on the roster, EA privacy hiding
        presence, the tracker started late) are absent from the result."""
        latest, ref, tie = self._this_match(names, at, anchor, after, need_legend=True)
        if tie:
            return {n: o["legend"] for n, o in latest.items() if o["since"] is None}
        if ref is None:
            return {n: o["legend"] for n, o in latest.items()}
        return {n: o["legend"] for n, o in latest.items()
                if o["since"] is None or abs(o["since"] - ref) <= _SINCE_TOLERANCE}

    def match_start(self, names, at=None, anchor=None, after=None):
        """Epoch the match that just ended at *at* began: the in-match since time its
        players share, picked the same way as for :meth:`match_legends`. None when no
        reading has one or the players split between two matches."""
        _, ref, _ = self._this_match(names, at, anchor, after, need_legend=False)
        return ref

    def _this_match(self, names, at, anchor, after, need_legend):
        """``(latest, ref, tie)`` for the match that just ended at *at*: each player's
        latest usable in-match reading, the since time that identifies the match
        (None when no reading has one), and whether two matches tied for it."""
        at = time.time() if at is None else float(at)
        with self._lock:
            latest = {}
            for n in dict.fromkeys(names):
                o = self._last_in_match(n, at)
                if o is None or (need_legend and not o["legend"]):
                    continue
                if after is not None and (o["ts"] < after if o["since"] is None
                                          else o["since"] < after - 60):
                    continue
                latest[n] = o
        sinces = [o["since"] for o in latest.values() if o["since"] is not None]
        if not sinces:
            return latest, None, False
        if anchor in latest and latest[anchor]["since"] is not None:
            return latest, latest[anchor]["since"], False
        votes = {s: sum(abs(t - s) <= _SINCE_TOLERANCE for t in sinces) for s in sinces}
        top = max(votes.values())
        clusters = {min(t for t in sinces if abs(t - s) <= _SINCE_TOLERANCE)
                    for s, v in votes.items() if v == top}
        if len(clusters) > 1:
            return latest, None, True
        return latest, next(s for s, v in votes.items() if v == top), False

    # ------------------------------------------------------------------ #
    # Map rotation — which map each playlist had when a match began
    # ------------------------------------------------------------------ #
    def map_at(self, mode, epoch):
        """The map *mode* (``"battle_royale"`` or ``"ranked"``) had at *epoch*, or
        None when no stored window covers it."""
        with self._rotation_lock:
            for w in (self._rotation.get(mode) or {}).values():
                if w["start"] <= epoch < w["end"]:
                    return w["map"]
        return None

    def match_maps(self, names, at=None, anchor=None, after=None):
        """``{mode: map}`` for the match that just ended at *at*, for every mode in
        :data:`ROTATION_MODES` (a value is None when unknown).

        The map is the one up when the match BEGAN, from the players' in-match since
        time: a game that starts just before a rotation is logged after it. With no
        since time, a map counts only when it was up for the whole span the match
        could have started in: the last :data:`_MATCH_MAX_SECS`, cut to after the
        previous logged match (*after*)."""
        at = time.time() if at is None else float(at)
        start = self.match_start(names, at=at, anchor=anchor, after=after)
        out = {}
        for mode in ROTATION_MODES:
            if start is not None:
                out[mode] = self.map_at(mode, start)
                continue
            lo = at - _MATCH_MAX_SECS
            if after is not None:
                lo = max(lo, float(after))
            first, last = self.map_at(mode, lo), self.map_at(mode, at)
            out[mode] = first if first is not None and first == last else None
        return out

    def _rotation_due(self, now):
        """Whether to fetch the rotation now (caller holds no lock)."""
        if not self._rotation_path or now < self._rotation_retry_at:
            return False
        if now - self._rotation_fetched >= _ROTATION_REFRESH:
            return True
        return any(self.map_at(m, now + _ROTATION_AHEAD) is None for m in ROTATION_MODES)

    def _fetch_rotation(self):
        """Fetch the ALS map rotation and store its windows. Never raises.

        Every attempt, good or bad, holds off the next one for _ROTATION_RETRY: when
        ALS answers but leaves a playlist uncovered (ranked between splits, no
        ``next``), the coverage check would otherwise ask again every poll cycle."""
        now = time.time()
        self._rotation_retry_at = now + _ROTATION_RETRY
        req = urllib.request.Request(_ALS_ROTATION, headers={
            "Authorization": self._api_key,
            "User-Agent": "apex-tracker",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            fetched = {m: _extract_windows(data, m) for m in ROTATION_MODES}
        except Exception as exc:
            _log(f"[RankTracker] map rotation fetch failed – {exc}")
            return
        if not any(fetched.values()):
            _log("[RankTracker] map rotation: ALS sent no usable windows")
            return
        self._merge_rotation(fetched, now)
        self._rotation_fetched = now
        if self._rotation_path:
            self._save_rotation()

    def _merge_rotation(self, fetched, now):
        """Store fetched windows. A fetch is the truth for the span it covers: stored
        windows overlapping it are replaced (ALS can reshuffle a schedule mid-split)."""
        with self._rotation_lock:
            for mode, wins in fetched.items():
                if not wins:
                    continue
                lo = min(w["start"] for w in wins)
                hi = max(w["end"] for w in wins)
                store = self._rotation.setdefault(mode, {})
                for s in [s for s, w in store.items() if w["start"] < hi and w["end"] > lo]:
                    del store[s]
                for w in wins:
                    store[w["start"]] = w
                for s in [s for s, w in store.items() if w["end"] < now - _ROTATION_KEEP]:
                    del store[s]

    def _load_rotation(self):
        """Seed the windows from rotation_path. A missing or bad file is ignored."""
        try:
            with open(self._rotation_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        with self._rotation_lock:
            for mode in ROTATION_MODES:
                for w in (saved.get(mode) or []) if isinstance(saved, dict) else []:
                    w = _clean_window(w)
                    if w and w["end"] >= now - _ROTATION_KEEP:
                        self._rotation[mode][w["start"]] = w

    def _save_rotation(self):
        """Write the windows to rotation_path (atomic replace). Never raises."""
        with self._rotation_lock:
            snap = {m: sorted(self._rotation[m].values(), key=lambda w: w["start"])
                    for m in ROTATION_MODES}
        tmp = self._rotation_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, self._rotation_path)
        except OSError as exc:
            _log(f"[RankTracker] map rotation save failed – {exc}")

    def in_match_recently(self, name, at=None):
        """Whether ALS saw *name* in a match within the last 45 min: True/False, or
        None when there is no presence data for them in that window (no ALS key,
        privacy settings, the tracker just started)."""
        at = time.time() if at is None else float(at)
        with self._lock:
            ring = self._presence.get(name) or ()
            recent = [o for o in ring if at - _MATCH_WINDOW <= o["ts"] <= at]
            if not recent or all(o["state"] is None for o in recent):
                return None
            return self._last_in_match(name, at) is not None

    # ------------------------------------------------------------------ #
    # RP ledger (§4.1) — session delta + resolver evidence
    # ------------------------------------------------------------------ #
    def set_session_start(self, epoch):
        """Mark the start of a watch run. Each player's session baseline is the
        first successful reading at/after this instant, so Start→Stop→Start begins
        a fresh session (the durable ledger keeps continuity for the resolver)."""
        with self._ledger_lock:
            self._session_start = float(epoch)

    def get_observations(self, name, since_epoch=0.0):
        """Ledger readings for *name* at ``ts >= since_epoch``, oldest first.

        Served from the in-memory ring buffer (hydrated from the ledger file at
        startup, so restarts keep history). Each entry is a copy of
        ``{"ts", "rp", "reset"}`` — the evidence the resolver classifies against.
        """
        with self._ledger_lock:
            ring = self._obs.get(name)
            if not ring:
                return []
            return [dict(o) for o in ring if o["ts"] >= since_epoch]

    def get_session_delta(self, name):
        """``(delta, baseline_ts, latest_ts)`` for *name* this session, or
        ``(None, None, None)`` if there is no reading yet.

        ``delta = latest.rp − baseline.rp`` where baseline is the first reading
        after the session start — re-anchored to the most recent reset within the
        session so a season rollover never poisons the number. This is the
        self-healing, always-correct session RP (TAS parity): any stale or missed
        reading is corrected by the next one, errors never accumulate."""
        with self._ledger_lock:
            ring = self._obs.get(name)
            sess = self._session_start
            if not ring:
                return None, None, None
            in_session = [o for o in ring if o["ts"] >= sess]
            if not in_session:
                return None, None, None
            baseline = in_session[0]
            for o in in_session:
                if o["reset"]:
                    baseline = o  # re-anchor past a reset
            latest = in_session[-1]
            return latest["rp"] - baseline["rp"], baseline["ts"], latest["ts"]

    def _note_notfound(self, name):
        """Record a 404 for *name* and warn ONCE if this roster name has never
        resolved — so a misspelled/dropped player surfaces immediately instead
        of silently logging match stats with no RP."""
        with self._lock:
            n = self._notfound_counts.get(name, 0) + 1
            self._notfound_counts[name] = n
            warn = (name not in self._ever_resolved
                    and n >= 2 and name not in self._warned)
            if warn:
                self._warned.add(name)
        _log(f"[RankTracker] {name}: 404 - player not found on {self._platform}")
        if warn:
            _log(f"[RankTracker] WARNING: '{name}' can't be found on ALS "
                 f"({self._platform}) after {n} tries - check the exact in-game "
                 f"spelling or add their UID in Settings; until then this "
                 f"player's RP will NOT be tracked.")

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
        self._maybe_fetch_rotation()
        while not self._stop.is_set():
            self._stop.wait(self._poll_seconds)
            if self._stop.is_set():
                break
            self._poll_all()
            self._maybe_fetch_rotation()

    def _maybe_fetch_rotation(self):
        """Fetch the map rotation when due, under _poll_lock so the call can't land
        in the same second as a player request (ALS rate limit)."""
        if self._stop.is_set() or not self._rotation_due(time.time()):
            return
        with self._poll_lock:
            self._fetch_rotation()

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
            learned_uid = _extract_uid(data)
            season = _extract_season(data)
            state, in_game = _extract_realtime(data)
            legend, since = _extract_presence(data)
            als_name = _extract_name(data)
            # EA/ALS occasionally serves a degraded response with rankScore=0 (or
            # a missing rank block → None) for a player who very much has RP.
            # Trusting it poisons everything downstream: a 0 baseline, bogus
            # ledger readings, false resets, fabricated deltas. A placed player's
            # real rankScore is never 0, so treat <=0 as a failed reading and KEEP
            # the last good cache value instead of overwriting it.
            if rp is not None and rp <= 0:
                _log(f"[RankTracker] {name}: ignoring rankScore={rp} "
                     f"(EA/ALS glitch; keeping last good reading)")
                rp = None
            newly_learned = None
            with self._lock:
                entry = self._cache.get(name)
                self._last_contact_ts = time.time()
                # Presence is recorded on every answer, usable RP or not.
                ring = self._presence.get(name)
                if ring is None:
                    ring = self._presence[name] = deque(maxlen=_PRESENCE_RING)
                ring.append({"ts": self._last_contact_ts, "state": state,
                             "since": since, "legend": legend})
                if als_name:
                    self._als_names[name] = als_name
                if rp is not None:
                    self._last_ok_ts = time.time()
                    self._last_error = None
                    prev = entry.get("current_rp") if entry else None
                    self._cache[name] = {
                        "current_rp": rp,
                        "previous_rp": prev,
                        "rank_name": rank_name,
                        "rank_div": rank_div,
                        "season": season,
                        "state": state,
                        "in_game": in_game,
                        "last_updated": time.time(),
                    }
                elif entry is None:
                    # First contact was degraded: record the player as resolved
                    # but leave RP empty (never store a bogus 0/None over data).
                    self._cache[name] = {
                        "current_rp": None, "previous_rp": None,
                        "rank_name": rank_name, "rank_div": rank_div,
                        "season": season, "state": state, "in_game": in_game,
                        "last_updated": time.time(),
                    }
                # else: keep the previous good entry untouched — a stale real
                # value beats a fresh bogus one for baselines and status alike.
                self._ever_resolved.add(name)
                self._notfound_counts.pop(name, None)
                self._warned.discard(name)
                # Auto-upgrade name -> UID: once ALS hands us this player's UID,
                # pin every future poll to it so we never depend on fragile name
                # search again (the #1 cause of "RP randomly missing").
                if learned_uid and name not in self._uids:
                    self._uids[name] = learned_uid
                    self._learned_uids[name] = learned_uid
                    newly_learned = learned_uid
            # Append to the always-on RP ledger (§4.1). Only readings with a real
            # rankScore are useful for deltas, so skip a None rp.
            if rp is not None:
                self._append_observation(name, self._uids.get(name), rp,
                                         rank_name, rank_div, season, state, in_game,
                                         legend=legend, since=since)
            if newly_learned:
                _log(f"[RankTracker] {name}: learned UID {newly_learned} "
                     f"-> future polls use it (no more name search)")
            _log(f"[RankTracker] {name}: RP={rp}  rank={rank_name} div={rank_div}")
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code != 404:  # a 404 is one unknown name, not an ALS problem
                with self._lock:
                    self._last_error = {
                        403: "ALS rejected the API key (403)",
                        429: "ALS rate limit (429)",
                    }.get(code, f"ALS HTTP {code}")
            if code == 400:
                _log(f"[RankTracker] {name}: 400 – EA API issue, will retry next cycle")
            elif code == 403:
                _log(f"[RankTracker] {name}: 403 – bad API key or unauthorized")
            elif code == 404:
                self._note_notfound(name)
            elif code == 429:
                _log(f"[RankTracker] {name}: 429 – rate limit; backing off")
                time.sleep(5.0)  # extra back-off
            else:
                _log(f"[RankTracker] {name}: HTTP {code}")
        except Exception as exc:
            with self._lock:
                self._last_error = f"ALS unreachable ({type(exc).__name__})"
            _log(f"[RankTracker] {name}: error – {exc}")

    # ------------------------------------------------------------------ #
    # Ledger internals
    # ------------------------------------------------------------------ #
    def _append_observation(self, name, uid, rp, tier, div, season, state, in_game,
                            legend=None, since=None):
        """Record one reading in the on-disk ledger and the in-memory ring.

        Flags a discontinuity (``"reset": true``) when the ranked season changes,
        or RP drops by more than ``reset_drop`` in one step alongside a tier drop —
        so a season rollover can't be misread as a huge match loss. Single writer
        (the poll thread); readers hold ``_ledger_lock``."""
        prev_season = self._last_season.get(name)
        prev_rp = self._last_rp.get(name)
        prev_tier = self._last_tier.get(name)

        season_changed = (prev_season is not None and season is not None
                          and season != prev_season)
        big_drop = (prev_rp is not None and (prev_rp - rp) > self._reset_drop)
        tier_dropped = _tier_index(tier) < _tier_index(prev_tier)
        # If either tier is unknown we can't confirm a tier drop, so a big drop
        # alone (which no single game can produce) is treated as a reset.
        tier_unknown = _tier_index(tier) < 0 or _tier_index(prev_tier) < 0
        reset = bool(season_changed or (big_drop and (tier_dropped or tier_unknown)))

        entry = {"ts": time.time(), "rp": rp, "reset": reset, "in_game": in_game}
        line = None
        # Ring + reset-detection state are per-instance (own lock).
        with self._ledger_lock:
            ring = self._obs.get(name)
            if ring is None:
                ring = self._obs[name] = deque(maxlen=_LEDGER_RING)
            ring.append(entry)
            self._last_season[name] = season if season is not None else prev_season
            self._last_rp[name] = rp
            self._last_tier[name] = tier if tier else prev_tier
            if self._ledger_path:
                line = json.dumps({
                    "ts": entry["ts"], "name": name, "uid": uid, "rp": rp,
                    "tier": tier or None, "div": div if div else None,
                    "season": season, "state": state, "in_game": in_game,
                    "legend": legend, "since": since,
                    **({"reset": True} if reset else {}),
                })
        # File append is under the SHARED lock (serializes vs a sibling instance's
        # rotate/hydrate), separate from the per-instance lock above (no nesting).
        if line is not None:
            with _LEDGER_FILE_LOCK:
                try:
                    with open(self._ledger_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError as exc:
                    _log(f"[RankTracker] ledger write failed – {exc}")
        if reset:
            _log(f"[RankTracker] {name}: RP reset detected (season/tier rollover) "
                 f"-> ledger discontinuity marked.")

    def _rotate_ledger(self, retention_days):
        """On startup, roll the ledger to ``rp_ledger.1.jsonl`` if it exceeds
        ~10 MB or is older than *retention_days* (one generation kept)."""
        path = self._ledger_path
        if not path or not os.path.exists(path):
            return
        with _LEDGER_FILE_LOCK:  # serialize vs a sibling instance's append/hydrate
            try:
                too_big = os.path.getsize(path) > _LEDGER_MAX_BYTES
                too_old = (time.time() - os.path.getmtime(path)) > retention_days * 86400
            except OSError:
                return
            if not (too_big or too_old):
                return
            backup = path[:-6] + ".1.jsonl" if path.endswith(".jsonl") else path + ".1"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
                _log(f"[RankTracker] rotated RP ledger -> {os.path.basename(backup)} "
                     f"({'size' if too_big else 'age'} threshold).")
            except OSError as exc:
                _log(f"[RankTracker] ledger rotation skipped – {exc}")

    def _hydrate_ledger(self):
        """Load the on-disk ledger into the per-player ring buffers at startup so
        restarts keep session/resolver history. Tolerant of partial/last-line
        corruption (a kill mid-write): bad lines are skipped."""
        path = self._ledger_path
        if not path or not os.path.exists(path):
            return
        loaded = 0
        try:
            with _LEDGER_FILE_LOCK, open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        ts = float(d["ts"])
                        rp = int(d["rp"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    name = d.get("name")
                    if not name:
                        continue
                    ring = self._obs.get(name)
                    if ring is None:
                        ring = self._obs[name] = deque(maxlen=_LEDGER_RING)
                    ring.append({"ts": ts, "rp": rp, "reset": bool(d.get("reset")),
                                 "in_game": d.get("in_game")})
                    if "legend" in d:  # lines from v1.6.9 on carry presence
                        pres = self._presence.get(name)
                        if pres is None:
                            pres = self._presence[name] = deque(maxlen=_PRESENCE_RING)
                        pres.append({"ts": ts, "state": d.get("state"),
                                     "since": d.get("since"), "legend": d.get("legend")})
                    # Seed reset-detection state so it continues across the restart.
                    self._last_rp[name] = rp
                    if d.get("season") is not None:
                        self._last_season[name] = d.get("season")
                    if d.get("tier"):
                        self._last_tier[name] = d.get("tier")
                    loaded += 1
        except OSError as exc:
            _log(f"[RankTracker] ledger hydrate skipped – {exc}")
            return
        if loaded:
            _log(f"[RankTracker] hydrated {loaded} RP ledger readings "
                 f"for {len(self._obs)} players.")


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


def _extract_uid(data):
    """``global.uid`` → str, or '' if absent. A non-empty value means ALS
    found the player, so we can pin future polls to this stable id."""
    try:
        uid = data["global"]["uid"]
    except (KeyError, TypeError):
        return ""
    return str(uid) if uid not in (None, "") else ""


def _extract_season(data):
    """``global.rank.rankedSeason`` → str, or None. Present only on some keys;
    used to detect a season rollover (a ledger reset boundary)."""
    try:
        s = data["global"]["rank"].get("rankedSeason")
    except (KeyError, TypeError, AttributeError):
        return None
    return str(s) if s not in (None, "") else None


def _extract_realtime(data):
    """The bridge ``realtime`` block → ``(currentState, isInGame)``.

    Server-side presence (a per-player match-boundary signal, no OCR). Only a
    *signal* — EA privacy settings can hide it — so tolerate absence gracefully.
    Returns ``(None, None)`` when the block is missing."""
    try:
        rt = data.get("realtime") or {}
    except AttributeError:
        return None, None
    state = rt.get("currentState")
    in_game = rt.get("isInGame")
    # ALS encodes isInGame as 0/1 in some responses; coerce to a clean bool/None.
    if isinstance(in_game, (int, float)):
        in_game = bool(in_game)
    return (str(state) if state not in (None, "") else None,
            in_game if isinstance(in_game, bool) else None)


def _extract_presence(data):
    """``realtime.selectedLegend`` and ``realtime.currentStateSinceTimestamp`` →
    ``(legend, since_epoch)``, each None when absent. In a match, selectedLegend
    is the legend being played (checked live against the squad on 2026-11-07)."""
    try:
        rt = data.get("realtime") or {}
    except AttributeError:
        return None, None
    legend = rt.get("selectedLegend")
    legend = str(legend).strip() if legend not in (None, "") else None
    try:
        since = float(rt.get("currentStateSinceTimestamp"))
    except (TypeError, ValueError):
        since = None
    return legend or None, (since if since and since > 0 else None)


def _extract_name(data):
    """``global.name`` → the player's current Apex name, or ''."""
    try:
        name = data["global"]["name"]
    except (KeyError, TypeError):
        return ""
    return str(name).strip() if name not in (None, "") else ""


def _clean_window(w):
    """One rotation window as ``{"start", "end", "map"}`` (epoch seconds), or None
    when it lacks a usable map name or time span."""
    try:
        start, end = int(w["start"]), int(w["end"])
        name = str(w["map"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if not name or end <= start:
        return None
    return {"start": start, "end": end, "map": name}


def _extract_windows(data, mode):
    """The ``current`` and ``next`` windows ALS gives for *mode* in a maprotation
    (version=2) response, cleaned; [] when the mode is missing."""
    try:
        block = data.get(mode) or {}
    except AttributeError:
        return []
    out = []
    for key in ("current", "next"):
        w = _clean_window(block.get(key) or {}) if isinstance(block, dict) else None
        if w:
            out.append(w)
    return out


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")
