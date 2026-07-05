"""Unit tests for the pure RP resolver (RP_TRACKING_V2_SPEC.md §5).

Runs under pytest, or standalone: ``py test_rp_resolver.py`` (no third-party deps
so it needs nothing beyond the stdlib and rp_resolver.py). Every §5 case is
covered: normal single match; slow propagation; genuine net-zero with hint
true/false/null; two matches with an intermediate observation (chaining); two
matches with none (ambiguity + one-ranked-one-pub); loss beyond the floor; reset
marker; restart/rehydrate; unrostered (no baseline) skip.
"""

from rp_resolver import resolve_rp, hint_of, _patch


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def rec(session, name, start, logged, hint=None, csv="apex_matches.csv", slot=1):
    return {"session_id": session, "name": name, "player_slot": slot,
            "starting_rp": start, "logged_epoch": logged,
            "ranked_hint": hint, "csv_path": csv}


def obs(ts, rp, reset=False):
    return {"ts": ts, "rp": rp, "reset": reset}


def flat_series(t0, rp, count, step=70):
    """`count` flat readings starting at t0, `step`s apart, all equal `rp`."""
    return [obs(t0 + i * step, rp) for i in range(count)]


def patch_for(res, session, name):
    for p in res.patches:
        if p["session_id"] == session and p["name"] == name:
            return p["set"]
    return None


DEFAULTS = dict(loss_floor=-100, zero_confirm_polls=6, zero_confirm_seconds=360)


# --------------------------------------------------------------------------- #
# rule 1 — changed reading (single match)
# --------------------------------------------------------------------------- #
def test_normal_single_match():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=None)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1100, 1000), obs(1200, 1030)]}
    res = resolve_rp(pending, observations, now=1300, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"ending_rp": 1030, "rp_change": 30, "ranked": True}
    assert ("A", "yzRobo") in res.resolved
    # latency recorded: first_changed_ts - logged_epoch
    assert res.latencies == [("A", "yzRobo", 200)]


def test_changed_reading_overrides_pub_hint():
    # Badge said pub (False), but RP moved → it was ranked. Delta wins.
    pending = [rec("A", "yzRobo", 500, 1000, hint=False)]
    observations = {"yzRobo": [obs(1200, 540)]}
    res = resolve_rp(pending, observations, now=1300, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo")["ranked"] is True
    assert patch_for(res, "A", "yzRobo")["rp_change"] == 40


def test_slow_propagation_waits_then_resolves():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True)]
    # Only a couple of flat readings so far: not enough to zero-confirm, no move.
    waiting = resolve_rp(pending, {"yzRobo": [obs(1030, 1000), obs(1090, 1000)]},
                         now=1100, **DEFAULTS)
    assert waiting.patches == [] and waiting.resolved == []
    # The delayed changed reading finally lands → clean resolution.
    later = resolve_rp(pending, {"yzRobo": [obs(1030, 1000), obs(1400, 1025)]},
                       now=1450, **DEFAULTS)
    assert patch_for(later, "A", "yzRobo") == {"ending_rp": 1025, "rp_change": 25, "ranked": True}


# --------------------------------------------------------------------------- #
# rule 2 — zero-confirm (genuine net-zero)
# --------------------------------------------------------------------------- #
def test_net_zero_hint_true():
    # Demotion-floor loss: RP pinned at a division floor, genuine 0, badge=ranked.
    pending = [rec("A", "yzRobo", 800, 1000, hint=True)]
    res = resolve_rp(pending, {"yzRobo": flat_series(1000, 800, 7)}, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"ending_rp": 800, "rp_change": 0, "ranked": True}
    assert ("A", "yzRobo") in res.resolved


def test_net_zero_hint_false_is_pub():
    pending = [rec("A", "yzRobo", 800, 1000, hint=False)]
    res = resolve_rp(pending, {"yzRobo": flat_series(1000, 800, 7)}, now=1500, **DEFAULTS)
    # Pub: mark ranked=false, leave RP null (never a fabricated 0).
    assert patch_for(res, "A", "yzRobo") == {"ranked": False}
    assert ("A", "yzRobo") in res.resolved


def test_net_zero_hint_none_is_unknown():
    pending = [rec("A", "yzRobo", 800, 1000, hint=None)]
    res = resolve_rp(pending, {"yzRobo": flat_series(1000, 800, 7)}, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") is None      # nothing written
    assert ("A", "yzRobo") in res.resolved            # but resolved (honest blank)
    assert any("unknown" in n for n in res.notes)


def test_zero_not_confirmed_waits_on_too_few_polls():
    pending = [rec("A", "yzRobo", 800, 1000, hint=True)]
    # Wide span but only 3 readings — not enough polls yet.
    res = resolve_rp(pending, {"yzRobo": [obs(1000, 800), obs(1200, 800), obs(1400, 800)]},
                     now=1450, **DEFAULTS)
    assert res.patches == [] and res.resolved == []


def test_zero_not_confirmed_waits_on_too_short_span():
    pending = [rec("A", "yzRobo", 800, 1000, hint=True)]
    # Enough readings but packed into < zero_confirm_seconds.
    res = resolve_rp(pending, {"yzRobo": flat_series(1000, 800, 7, step=10)},
                     now=1100, **DEFAULTS)
    assert res.patches == [] and res.resolved == []


# --------------------------------------------------------------------------- #
# chaining — two matches with an intermediate observation
# --------------------------------------------------------------------------- #
def test_two_matches_intermediate_obs_chains_baseline():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),
               rec("B", "yzRobo", 1000, 2000, hint=True)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1200, 1030),
                               obs(1500, 1030), obs(2100, 1055)]}
    res = resolve_rp(pending, observations, now=2200, **DEFAULTS)
    # Earliest match resolves against its isolated move…
    assert patch_for(res, "A", "yzRobo") == {"ending_rp": 1030, "rp_change": 30, "ranked": True}
    # …and the next match's baseline chains to the window-end value (last reading
    # before B was logged), NOT the post-B reading.
    assert res.rebaselines[("B", "yzRobo")] == 1030
    assert ("B", "yzRobo") not in res.resolved        # B waits for its own sweep


# --------------------------------------------------------------------------- #
# rule 3 — two matches collapsed, no separating observation
# --------------------------------------------------------------------------- #
def test_collapsed_one_ranked_one_pub_attributes_to_ranked():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),    # ranked
               rec("B", "yzRobo", 1000, 1100, hint=False)]   # pub
    # No reading isolates A before B is logged; the move shows up only later.
    observations = {"yzRobo": [obs(1000, 1000), obs(1400, 1040)]}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"ending_rp": 1040, "rp_change": 40, "ranked": True}
    assert patch_for(res, "B", "yzRobo") == {"ranked": False}
    assert set(res.resolved) == {("A", "yzRobo"), ("B", "yzRobo")}


def test_collapsed_pub_then_ranked_attributes_to_second():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=False),   # pub
               rec("B", "yzRobo", 1000, 1100, hint=True)]    # ranked
    observations = {"yzRobo": [obs(1000, 1000), obs(1400, 1040)]}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"ranked": False}
    assert patch_for(res, "B", "yzRobo") == {"ending_rp": 1040, "rp_change": 40, "ranked": True}


def test_collapsed_ambiguous_leaves_both_blank():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),
               rec("B", "yzRobo", 1000, 1100, hint=True)]    # both ranked → can't split
    observations = {"yzRobo": [obs(1000, 1000), obs(1400, 1040)]}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert res.patches == []                                 # nothing fabricated
    assert set(res.resolved) == {("A", "yzRobo"), ("B", "yzRobo")}
    assert any("collapsed" in n for n in res.notes)


# --------------------------------------------------------------------------- #
# floor + reset guards
# --------------------------------------------------------------------------- #
def test_loss_beyond_floor_blank():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1200, 850)]}   # -150 < -100 floor
    res = resolve_rp(pending, observations, now=1300, **DEFAULTS)
    assert res.patches == []
    assert ("A", "yzRobo") in res.resolved
    assert any("single-game loss" in n for n in res.notes)


def test_reset_marker_blanks_and_reanchors_next():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),
               rec("B", "yzRobo", 1000, 1300, hint=True)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1200, 120, reset=True)]}
    res = resolve_rp(pending, observations, now=1400, **DEFAULTS)
    assert res.patches == []                                 # no delta across a reset
    assert ("A", "yzRobo") in res.resolved
    assert res.rebaselines[("B", "yzRobo")] == 120           # re-anchored post-reset
    assert any("reset" in n for n in res.notes)


def test_reset_reanchors_next_to_window_end_not_latest():
    # A post-reset observation exists that already includes B's own outcome (+25):
    # B must re-anchor to the reading BEFORE it was logged (120), not the latest (145).
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),
               rec("B", "yzRobo", 120, 1250, hint=True)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1200, 120, reset=True), obs(1400, 145)]}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert res.patches == []
    assert res.rebaselines[("B", "yzRobo")] == 120           # NOT 145


def test_three_way_collapse_blanks_third_match():
    # Three games logged inside one propagation bracket; a single reading covers
    # all three. None can be split → all three blanked (no fabricated split), and
    # the third must NOT be chained to the contaminated reading (which would later
    # zero-confirm it to a false 0).
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),
               rec("B", "yzRobo", 1000, 1010, hint=True),
               rec("C", "yzRobo", 1000, 1020, hint=True)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1400, 1060)]}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert res.patches == []
    assert set(res.resolved) == {("A", "yzRobo"), ("B", "yzRobo"), ("C", "yzRobo")}
    assert ("C", "yzRobo") not in res.rebaselines
    assert any("collapse" in n for n in res.notes)


def test_collapse_then_genuinely_later_match_chains():
    # Same collapse for A+B, but C was logged well AFTER the covering reading, so
    # it IS separable → chain it to the observed value (resolves on its own sweep).
    pending = [rec("A", "yzRobo", 1000, 1000, hint=True),
               rec("B", "yzRobo", 1000, 1010, hint=True),
               rec("C", "yzRobo", 1000, 1600, hint=True)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1400, 1060)]}
    res = resolve_rp(pending, observations, now=1700, **DEFAULTS)
    assert res.rebaselines[("C", "yzRobo")] == 1060
    assert ("C", "yzRobo") not in res.resolved


# --------------------------------------------------------------------------- #
# restart / rehydrate — resolver is stateless, resolves from passed-in data
# --------------------------------------------------------------------------- #
def test_rehydrated_queue_resolves_from_data_only():
    # Simulate an app restart: the pending record + the ledger observations are
    # reloaded from disk and handed straight to the resolver. No process state.
    pending = [rec("OLD", "SaWaii", 9000, 500, hint=None)]
    observations = {"SaWaii": [obs(400, 9000), obs(700, 9048)]}
    res = resolve_rp(pending, observations, now=99999, **DEFAULTS)
    assert patch_for(res, "OLD", "SaWaii") == {"ending_rp": 9048, "rp_change": 48, "ranked": True}


# --------------------------------------------------------------------------- #
# unrostered player — no baseline, resolver skips (warning is emitted at log time)
# --------------------------------------------------------------------------- #
def test_no_baseline_is_skipped():
    pending = [rec("A", "SomeRandom", None, 1000, hint=True)]
    res = resolve_rp(pending, {"SomeRandom": [obs(1200, 1234)]}, now=1300, **DEFAULTS)
    assert res.patches == [] and res.resolved == [] and res.notes == []


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_hint_prefers_v2_key_but_falls_back():
    assert hint_of({"ranked_hint": True, "ranked": False}) is True   # v2 wins
    assert hint_of({"ranked": False}) is False                        # v1 fallback
    assert hint_of({}) is None


def test_patch_keeps_falsy_but_valid_values():
    s = _patch({"session_id": "A", "csv_path": "x"}, "n",
               ending_rp=0, rp_change=0, ranked=False)["set"]
    assert s == {"ending_rp": 0, "rp_change": 0, "ranked": False}
    # None means "don't touch this column".
    assert _patch({"session_id": "A"}, "n", ranked=True)["set"] == {"ranked": True}


# --------------------------------------------------------------------------- #
# standalone runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}  {e!r}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}  {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed.")
    sys.exit(1 if failed else 0)
