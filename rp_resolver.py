"""Pure RP resolution logic for the v2 session-ledger design.

Resolve-time classification (RP_TRACKING_V2_SPEC.md §4.2): every logged match is
queued for RP resolution (never gated by the ranked badge at log time), and this
module classifies each player's earliest pending match against the always-on RP
ledger *at resolve time*, when there is real evidence to work with:

  1. a nonzero observed delta proves the match was ranked (overriding the badge);
  2. a zero delta confirmed by stable readings over a full propagation window is a
     genuine net-zero (badge is only the tiebreaker for the zero case);
  3. two matches collapsed into one reading with no separating observation are
     flagged, not guessed.

Hard rule carried through every branch: **never fabricate an RP value.** Every
number written is a directly observed delta or a zero-confirmed 0; everything
else stays null (the match row itself is never touched — only RP attribution).

This module is intentionally dependency-free (stdlib only) so the resolver is
unit-testable without importing the heavy OCR/GUI stack in apex_tracker.py. The
impure side (loading the pending queue, polling ALS, reading the ledger, writing
the CSV/Supabase) lives in apex_tracker.py and drives this with plain data.
"""

from dataclasses import dataclass, field


@dataclass
class RpResolution:
    """The actions a resolve pass wants applied. Pure data — the caller performs
    the I/O (patch CSV/Supabase, rewrite the pending queue, print notes).

    patches      -- column writes: [{session_id, name, csv_path, set: {col: val}}].
                    ``set`` holds ONLY the columns to write (any of ending_rp /
                    rp_change / ranked); columns absent from ``set`` are left
                    untouched, so a real value is never overwritten with a
                    fabricated one and a pub's null RP is never stamped 0.
    resolved     -- [(session_id, name)] to DROP from the pending queue (a final
                    decision was reached — whether a value was written or not).
    rebaselines  -- {(session_id, name): starting_rp} chained baselines: the next
                    pending match starts where this one's observed value now sits.
    notes        -- console lines describing honest blanks / ambiguity.
    latencies    -- [(session_id, name, seconds)] propagation latency per resolved
                    match (first_changed_ts - logged_epoch), for cadence tuning.
    chain_updates-- {name: {"end": rp, "ts": obs_ts}} per-player anchors after each
                    resolution: the last OBSERVED value/timestamp the resolution
                    ended on. The caller persists these (rp_chain.json) and passes
                    them back as ``chain`` so the next match's baseline can be
                    corrected even across sweeps, restarts, or a cleared queue —
                    a frozen cache snapshot alone is contaminable (EA propagation
                    lag, a squadmate's RP posting early when they leave a match
                    before the summary screen is read).
    """

    patches: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    rebaselines: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    latencies: list = field(default_factory=list)
    chain_updates: dict = field(default_factory=dict)


def hint_of(rec):
    """The ranked-badge verdict stored on a pending record: True / False / None.

    Reads the v2 ``ranked_hint`` key, falling back to the v1 ``ranked`` key so a
    queue written by an older build still resolves after an upgrade.
    """
    if "ranked_hint" in rec:
        return rec["ranked_hint"]
    return rec.get("ranked")


def _patch(rec, name, *, ending_rp=None, rp_change=None, ranked=None):
    """Build a patch entry that writes ONLY the provided columns.

    ``is not None`` guards keep a legitimate ``rp_change=0`` and ``ranked=False``
    (both falsy) while dropping the "don't touch this column" sentinel None.
    """
    columns = {}
    if ending_rp is not None:
        columns["ending_rp"] = ending_rp
    if rp_change is not None:
        columns["rp_change"] = rp_change
    if ranked is not None:
        columns["ranked"] = ranked
    return {
        "session_id": rec["session_id"],
        "name": name,
        "csv_path": rec.get("csv_path"),
        "set": columns,
    }


# Zero-confirm requires evidence the baseline existed BEFORE the match ended:
# an observation at least this many seconds pre-log equal to the baseline. Without
# it (tracker started on/after the summary screen), the frozen baseline may
# already contain the match's own result, and "no movement" proves nothing.
PRE_OBS_MARGIN = 60
# A chain anchor older than this is stale (overnight / untracked play in between).
CHAIN_MAX_AGE = 7200
# Observed RP movement between the chain anchor and this many seconds before the
# match's log time means someone played an UNTRACKED game in the gap — the chain
# can't vouch for the baseline. Movement inside the final window is different:
# that's plausibly this match's own result posting early (a player who dies and
# leaves finalizes RP before the squad's summary screen is read) and is exactly
# what the chain exists to see through. ~ one long match.
CHAIN_QUIET_WINDOW = 1500


def resolve_rp(pending, observations, now, *, loss_floor=-100,
               zero_confirm_polls=6, zero_confirm_seconds=360, chain=None):
    """Classify each player's earliest pending match against the ledger.

    Parameters
    ----------
    pending : list[dict]
        Fresh (non-expired) pending records, each::

            {"session_id", "name", "player_slot", "starting_rp",
             "ranked_hint" | "ranked", "csv_path", "logged_epoch"}

    observations : dict[str, list[dict]]
        Per-player successful ledger readings ``{name: [{"ts", "rp", "reset"},
        ...]}`` (any order; sorted here). ``reset`` marks a season rollover / RP
        reset discontinuity that deltas must not be computed across.
    now : float
        Current epoch (reserved for the caller's bookkeeping; classification is
        driven entirely by observation timestamps so the result is deterministic).
    loss_floor : int
        Most negative believable single-game RP loss. A larger drop can't be one
        game, so it's treated as collapsed/glitched and left blank.
    zero_confirm_polls, zero_confirm_seconds : int
        A flat reading is only a *genuine* 0 once at least this many successful
        observations, spanning at least this many seconds after the match, all
        equal the baseline (replaces the old infinite ``current == start`` wait).
    chain : dict | None
        ``{name: {"end": rp, "ts": obs_ts}}`` — each player's previous resolution
        anchor (persisted by the caller). When fresh and uncontradicted it
        OVERRIDES the record's frozen ``starting_rp``: the frozen cache snapshot
        can be stale (previous match not yet propagated → the same movement gets
        attributed to two matches) or contaminated (this match's own result posted
        early via a player who left before the summary was read → false 0).

    Returns
    -------
    RpResolution
        Pure description of patches / resolutions / rebaselines / notes.

    The function is pure: no I/O, no globals, deterministic for given inputs.
    It resolves each player's EARLIEST pending match (chaining the next one's
    baseline forward), mirroring the durable-queue model the sweep already uses.
    """
    res = RpResolution()
    by_name = {}
    for r in pending:
        if r.get("starting_rp") is None:
            continue  # no baseline (ALS hadn't resolved them) — nothing to do
        by_name.setdefault(r["name"], []).append(r)

    for name, recs in by_name.items():
        recs = sorted(recs, key=lambda r: r.get("logged_epoch", 0.0))
        obs = sorted(observations.get(name, []), key=lambda o: o["ts"])
        anchor = (chain or {}).get(name)
        _resolve_one_player(name, recs, obs, anchor, res, loss_floor,
                            zero_confirm_polls, zero_confirm_seconds)
    return res


def _effective_baseline(name, rec0, obs, anchor, res):
    """The baseline to resolve against: the persisted chain anchor when it is
    fresh and uncontradicted, else the record's frozen ``starting_rp``.

    The chain wins only when ALL hold — otherwise the frozen snapshot, for all
    its faults, is the honest fallback:
      * anchored at/before this match was logged, and not stale (CHAIN_MAX_AGE);
      * no reset discontinuity between the anchor and the log;
      * no observed movement in the gap OLDER than CHAIN_QUIET_WINDOW before the
        log (that would mean an untracked game whose delta the chain would
        otherwise misattribute to this match; movement INSIDE the final window is
        plausibly this match's own early-posted result and is chained through).
    """
    start = rec0["starting_rp"]
    t0 = rec0.get("logged_epoch", 0.0)
    if not anchor:
        return start
    ts, end = anchor.get("ts"), anchor.get("end")
    if ts is None or end is None or ts > t0 or (t0 - ts) > CHAIN_MAX_AGE:
        return start
    between = [o for o in obs if ts < o["ts"] <= t0]
    if any(o.get("reset") for o in between):
        return start
    if any(o["rp"] != end and o["ts"] <= t0 - CHAIN_QUIET_WINDOW for o in between):
        return start  # untracked play in the gap — chain can't vouch
    if end != start:
        res.notes.append(
            f"{rec0['session_id']} / {name}: baseline corrected "
            f"{start} -> {end} (chained from the previous resolution; the frozen "
            f"snapshot was stale or already contained this match's result).")
    return end


def _resolve_one_player(name, recs, obs, anchor, res, loss_floor, zc_polls, zc_seconds):
    """Resolve the earliest pending match for one player against its ledger."""
    rec0 = recs[0]
    rec1 = recs[1] if len(recs) > 1 else None
    start = _effective_baseline(name, rec0, obs, anchor, res)
    t0 = rec0.get("logged_epoch", 0.0)
    # Evidence the baseline predates the match's end: an observation comfortably
    # before log time that equals it. Without this, "readings never moved" cannot
    # distinguish a genuine 0 from a baseline that already contained the result.
    pre_ok = any(o["ts"] <= t0 - PRE_OBS_MARGIN and o["rp"] == start for o in obs)

    # Only readings taken at/after the match was logged can reflect its outcome.
    post = [o for o in obs if o["ts"] >= t0]
    if not post:
        return  # no reading since the match yet — wait for the next sweep

    # Walk forward for the first discontinuity: a reset (season rollover) takes
    # precedence over a normal change even though a reset IS a big RP move, because
    # we must refuse to compute a delta across it.
    changed = None
    reset_first = False
    for o in post:
        if o.get("reset"):
            reset_first = True
            break
        if o["rp"] != start:
            changed = o
            break

    if reset_first:
        key0 = (rec0["session_id"], name)
        res.notes.append(
            f"{rec0['session_id']} / {name}: baseline spans an RP reset "
            f"(season rollover) - left blank (no delta across a reset).")
        res.resolved.append(key0)
        # Anchor the chain at the last post-reset observation.
        res.chain_updates[name] = {"end": post[-1]["rp"], "ts": post[-1]["ts"]}
        if rec1 is not None:
            # Re-anchor the next match to the value it actually STARTED from — the
            # last reading before rec1 was logged (post-reset) — not the latest
            # reading, which may already include rec1's own outcome. Same
            # windowing contract as the clean changed-path below.
            t1 = rec1.get("logged_epoch", 0.0)
            window = [o["rp"] for o in post if o["ts"] < t1]
            res.rebaselines[(rec1["session_id"], rec1["name"])] = (
                window[-1] if window else post[-1]["rp"])
        return

    if changed is not None:
        _resolve_changed(name, rec0, rec1, recs, start, changed, post, res, loss_floor)
        return

    # No change and no reset: every post-baseline reading equals `start` → a
    # zero-delta candidate (rule 2).
    _resolve_zero(name, rec0, rec1, recs, start, t0, post, pre_ok, res,
                  zc_polls, zc_seconds)


def _resolve_changed(name, rec0, rec1, recs, start, changed, post, res, loss_floor):
    """Rule 1 (clean single match) / rule 3 (collapsed pair) for an observed move."""
    key0 = (rec0["session_id"], name)
    delta = changed["rp"] - start
    # Whatever the outcome, the world was OBSERVED at `changed` — anchor the chain
    # there so the next match's baseline can be corrected against it.
    res.chain_updates[name] = {"end": changed["rp"], "ts": changed["ts"]}

    if rec1 is not None and changed["ts"] >= rec1.get("logged_epoch", 0.0):
        # The move wasn't observed before the next match was logged, so this one
        # reading covers BOTH games and cannot be split (rule 3).
        _resolve_collapsed(name, rec0, rec1, recs, start, changed, delta, res, loss_floor)
        return

    # Clean single-match resolution (rule 1): a nonzero delta proves it was ranked.
    if delta < loss_floor:
        # A single game can't lose this much: an untracked game collapsed in, or an
        # ALS glitch. Don't fabricate a value — leave RP blank (row itself untouched).
        res.notes.append(
            f"{rec0['session_id']} / {name}: observed {delta:+d} exceeds a single-game "
            f"loss (untracked game or ALS glitch) - left blank (no fabricated value).")
        res.resolved.append(key0)
    else:
        res.patches.append(_patch(rec0, name, ending_rp=changed["rp"],
                                  rp_change=delta, ranked=True))
        res.resolved.append(key0)
        res.latencies.append((rec0["session_id"], name,
                              changed["ts"] - rec0.get("logged_epoch", 0.0)))

    if rec1 is not None:
        # Chain: the next match's baseline is rec0's exclusive-window end value (the
        # last reading before rec1 was logged; `changed` itself qualifies here).
        t1 = rec1.get("logged_epoch", 0.0)
        window = [o["rp"] for o in post if o["ts"] < t1]
        res.rebaselines[(rec1["session_id"], rec1["name"])] = (
            window[-1] if window else changed["rp"])


def _resolve_collapsed(name, rec0, rec1, recs, start, changed, delta, res, loss_floor):
    """Rule 3: two pending matches collapsed into one reading, no observation
    separating them. Attribute the whole delta only when exactly one is ranked and
    the other a pub; otherwise leave both blank. Never fabricate a split.

    ``changed`` is None in the zero-delta variant (both games flat); ``delta`` is
    then 0 and ``endpoint`` is the (unchanged) baseline.
    """
    key0 = (rec0["session_id"], name)
    key1 = (rec1["session_id"], name)
    h0 = hint_of(rec0)
    h1 = hint_of(rec1)
    endpoint = changed["rp"] if changed is not None else start

    if h0 is True and h1 is False and delta >= loss_floor:
        res.patches.append(_patch(rec0, name, ending_rp=endpoint,
                                  rp_change=delta, ranked=True))
        res.patches.append(_patch(rec1, name, ranked=False))
        res.resolved += [key0, key1]
    elif h0 is False and h1 is True and delta >= loss_floor:
        res.patches.append(_patch(rec0, name, ranked=False))
        res.patches.append(_patch(rec1, name, ending_rp=endpoint,
                                  rp_change=delta, ranked=True))
        res.resolved += [key0, key1]
    else:
        res.notes.append(
            f"{rec0['session_id']} & {rec1['session_id']} / {name}: two matches "
            f"collapsed into one RP reading ({delta:+d}) with no separating "
            f"observation - left blank (no fabricated split).")
        res.resolved += [key0, key1]

    # Any FURTHER pending match logged before this covering reading is part of the
    # SAME inseparable collapse (3+ games in one bracket) → blank it too, never
    # fabricate a split. The first match logged AFTER the reading is clean → chain
    # it to the observed value and stop (later ones resolve on subsequent sweeps).
    if changed is not None:
        for recN in recs[2:]:
            if recN.get("logged_epoch", 0.0) <= changed["ts"]:
                res.notes.append(
                    f"{recN['session_id']} / {name}: part of a multi-match RP "
                    f"collapse with no separating observation - left blank.")
                res.resolved.append((recN["session_id"], name))
            else:
                res.rebaselines[(recN["session_id"], name)] = changed["rp"]
                break


def _resolve_zero(name, rec0, rec1, recs, start, t0, post, pre_ok, res,
                  zc_polls, zc_seconds):
    """Rule 2 (zero-confirm): a flat reading is a *genuine* 0 only once enough
    stable observations span a full propagation window AND the baseline is known
    to predate the match (``pre_ok``). Caller guarantees every reading in ``post``
    equals ``start`` (no change, no reset).
    """
    enough_polls = len(post) >= zc_polls
    enough_span = (post[-1]["ts"] - t0) >= zc_seconds
    if not (enough_polls and enough_span):
        return  # keep waiting; RP may still be propagating (EA cache lag)

    # In-game guard: EA/ALS serves a player's STALE pre-match rankScore while they
    # are IN a match, so a flat reading from an in-match player proves nothing —
    # their real result hasn't posted yet. If we have presence data (isInGame) and
    # have NOT yet seen this value while they were OUT of a match (settled in the
    # lobby), keep waiting. This is the exact failure mode of a player who dies
    # early and instantly re-queues: perpetually in-match, RP frozen for several
    # games, then a single collapsed jump. When presence is absent (EA privacy
    # hides realtime → all None), the guard is skipped and behaviour is unchanged.
    have_presence = any(o.get("in_game") is not None for o in post)
    settled = any(o.get("in_game") is False for o in post)
    if have_presence and not settled:
        return  # RP flat but the player is still in-match — reading is stale, wait

    # The chain anchors at the confirmed-flat value either way.
    res.chain_updates[name] = {"end": start, "ts": post[-1]["ts"]}

    if not pre_ok:
        # No observation shows this baseline existed BEFORE the match ended (e.g.
        # the tracker was started on the summary screen), so the frozen value may
        # already include the match's own result — "no movement" proves nothing.
        # Resolve as honest unknown rather than stamping a fabricated 0.
        res.notes.append(
            f"{rec0['session_id']} / {name}: readings flat, but the baseline was "
            f"never observed before the match ended (tracker started too late?) - "
            f"can't distinguish a genuine 0 from an already-included result; "
            f"left blank.")
        res.resolved.append((rec0["session_id"], name))
        return

    if rec1 is not None:
        # Need to have observed rec0's OWN window flat (a reading before rec1 was
        # logged); else we can't isolate rec0 from rec1 → treat as a collapsed pair.
        observed_before_rec1 = any(
            o["ts"] < rec1.get("logged_epoch", 0.0) for o in post)
        if not observed_before_rec1:
            # Zero total across an inseparable pair: attribute via rule 3 (the loss
            # floor is irrelevant for a 0 delta, so it can't trip).
            _resolve_collapsed(name, rec0, rec1, recs, start, None, 0, res,
                               loss_floor=start - 10 ** 9)
            return

    _apply_zero(rec0, name, hint_of(rec0), start, res)
    # rec1 keeps its baseline (RP hasn't moved) — no rebaseline needed.


def _apply_zero(rec, name, hint, start, res):
    """Resolve a zero-confirmed match by the badge tiebreaker (rule 2)."""
    key = (rec["session_id"], name)
    if hint is True:
        # Real ranked net-zero (demotion-protection pins RP at a division floor):
        # a confirmed 0 is a legitimate resolved value.
        res.patches.append(_patch(rec, name, ending_rp=start, rp_change=0, ranked=True))
        res.resolved.append(key)
    elif hint is False:
        # No movement + badge says pub → it's a pub. Leave RP null (not a 0).
        res.patches.append(_patch(rec, name, ranked=False))
        res.resolved.append(key)
    else:
        # No movement + no badge signal → honestly unknown. Drop without a value.
        res.notes.append(
            f"{rec['session_id']} / {name}: no RP movement confirmed and no ranked "
            f"badge signal - resolved as unknown (left blank).")
        res.resolved.append(key)
