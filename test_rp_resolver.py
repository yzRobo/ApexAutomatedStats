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


def obs(ts, rp, reset=False, in_game=None):
    return {"ts": ts, "rp": rp, "reset": reset, "in_game": in_game}


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
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 1000, "ending_rp": 1030, "rp_change": 30, "ranked": True}
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
    assert patch_for(later, "A", "yzRobo") == {"starting_rp": 1000, "ending_rp": 1025, "rp_change": 25, "ranked": True}


# --------------------------------------------------------------------------- #
# rule 2 — zero-confirm (genuine net-zero)
# --------------------------------------------------------------------------- #
def test_net_zero_hint_true():
    # Demotion-floor loss: RP pinned at a division floor, genuine 0, badge=ranked.
    # The pre-match observation proves the baseline predates the match's end.
    pending = [rec("A", "yzRobo", 800, 1000, hint=True)]
    observations = {"yzRobo": [obs(900, 800)] + flat_series(1000, 800, 7)}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 800, "ending_rp": 800, "rp_change": 0, "ranked": True}
    assert ("A", "yzRobo") in res.resolved


def test_net_zero_hint_false_is_pub():
    pending = [rec("A", "yzRobo", 800, 1000, hint=False)]
    observations = {"yzRobo": [obs(900, 800)] + flat_series(1000, 800, 7)}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    # Pub: mark ranked=false, leave RP null (never a fabricated 0).
    assert patch_for(res, "A", "yzRobo") == {"ranked": False}
    assert ("A", "yzRobo") in res.resolved


def test_net_zero_hint_none_is_unknown():
    pending = [rec("A", "yzRobo", 800, 1000, hint=None)]
    observations = {"yzRobo": [obs(900, 800)] + flat_series(1000, 800, 7)}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") is None      # nothing written
    assert ("A", "yzRobo") in res.resolved            # but resolved (honest blank)
    assert any("unknown" in n for n in res.notes)


def test_zero_deferred_while_player_still_in_match():
    # Early-leaver / instant-requeue: readings are flat but the player is still
    # IN a match, so EA is serving stale pre-match RP — do NOT zero-confirm yet.
    pending = [rec("A", "TCremer22", 8500, 1000, hint=True)]
    observations = {"TCremer22": [obs(900, 8500, in_game=True)]
                    + [obs(1000 + i * 70, 8500, in_game=True) for i in range(7)]}
    res = resolve_rp(pending, observations, now=1600, **DEFAULTS)
    assert res.patches == [] and res.resolved == []   # waiting, not a false 0


def test_zero_confirms_once_settled_out_of_match():
    # Same flat readings, but the last one caught the player OUT of the match
    # (in the lobby) at the same RP → now it's a genuine, settled 0.
    pending = [rec("A", "TCremer22", 8500, 1000, hint=True)]
    observations = {"TCremer22": [obs(900, 8500, in_game=True)]
                    + [obs(1000 + i * 70, 8500, in_game=True) for i in range(6)]
                    + [obs(1500, 8500, in_game=False)]}
    res = resolve_rp(pending, observations, now=1600, **DEFAULTS)
    assert patch_for(res, "A", "TCremer22") == {"starting_rp": 8500, "ending_rp": 8500, "rp_change": 0, "ranked": True}


def test_zero_guard_skipped_when_presence_hidden():
    # EA privacy hides realtime (in_game all None) → guard can't apply, behaviour
    # is unchanged (resolves on the pre-match + span evidence like before).
    pending = [rec("A", "yzRobo", 800, 1000, hint=True)]
    observations = {"yzRobo": [obs(900, 800)] + flat_series(1000, 800, 7)}  # in_game=None
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 800, "ending_rp": 800, "rp_change": 0, "ranked": True}


def test_zero_without_pre_match_observation_is_unknown():
    # Tracker started on/after the summary screen: the frozen baseline may already
    # contain the match's result, so a flat reading proves nothing. Honest blank,
    # NOT a fabricated 0 — this was the "first game of the night is 0" bug.
    pending = [rec("A", "yzRobo", 800, 1000, hint=True)]
    res = resolve_rp(pending, {"yzRobo": flat_series(1000, 800, 7)}, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") is None
    assert ("A", "yzRobo") in res.resolved
    assert any("never observed before" in n for n in res.notes)


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
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 1000, "ending_rp": 1030, "rp_change": 30, "ranked": True}
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
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 1000, "ending_rp": 1040, "rp_change": 40, "ranked": True}
    assert patch_for(res, "B", "yzRobo") == {"ranked": False}
    assert set(res.resolved) == {("A", "yzRobo"), ("B", "yzRobo")}


def test_collapsed_pub_then_ranked_attributes_to_second():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=False),   # pub
               rec("B", "yzRobo", 1000, 1100, hint=True)]    # ranked
    observations = {"yzRobo": [obs(1000, 1000), obs(1400, 1040)]}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"ranked": False}
    assert patch_for(res, "B", "yzRobo") == {"starting_rp": 1000, "ending_rp": 1040, "rp_change": 40, "ranked": True}


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
    assert patch_for(res, "OLD", "SaWaii") == {"starting_rp": 9000, "ending_rp": 9048, "rp_change": 48, "ranked": True}


# --------------------------------------------------------------------------- #
# unrostered player — no baseline, resolver skips (warning is emitted at log time)
# --------------------------------------------------------------------------- #
def test_no_baseline_is_skipped():
    pending = [rec("A", "SomeRandom", None, 1000, hint=True)]
    res = resolve_rp(pending, {"SomeRandom": [obs(1200, 1234)]}, now=1300, **DEFAULTS)
    assert res.patches == [] and res.resolved == [] and res.notes == []


# --------------------------------------------------------------------------- #
# persistent chain — baseline correction across sweeps/restarts
# --------------------------------------------------------------------------- #
def test_chain_corrects_stale_baseline_no_double_attribution():
    # Match A (already resolved, -65 at obs ts=1250) then match B logged at 1300
    # with a STALE frozen baseline (12335: EA hadn't propagated A's loss when B's
    # cache snapshot froze). Without the chain, B resolves off the same movement
    # as A (-65 duplicated, or -130 floor-blanked). With the chain anchor, B's
    # baseline is corrected to A's observed end and B gets ITS OWN delta.
    pending = [rec("B", "yzRobo", 12335, 1300, hint=True)]
    observations = {"yzRobo": [obs(1100, 12335), obs(1250, 12270), obs(1400, 12205)]}
    chain = {"yzRobo": {"end": 12270, "ts": 1250}}
    res = resolve_rp(pending, observations, now=1500, chain=chain, **DEFAULTS)
    assert patch_for(res, "B", "yzRobo") == {"starting_rp": 12270, "ending_rp": 12205, "rp_change": -65, "ranked": True}
    assert any("baseline corrected" in n for n in res.notes)


def test_chain_early_leaver_gain_attributed_not_zero():
    # A player who dies and leaves has their RP post BEFORE the squad's summary
    # screen is read, so the frozen baseline already contains their +27 and the
    # readings sit flat afterwards — v1.6.1 stamped a false 0. The chain anchors
    # at the previous resolution's end, exposing the +27 as this match's delta.
    pending = [rec("M", "NameJFK", 10053, 1300, hint=True)]
    observations = {"NameJFK": [obs(940, 10026), obs(1000, 10053), obs(1100, 10053)]
                    + flat_series(1300, 10053, 7)}
    chain = {"NameJFK": {"end": 10026, "ts": 950}}
    res = resolve_rp(pending, observations, now=1900, chain=chain, **DEFAULTS)
    assert patch_for(res, "M", "NameJFK") == {"starting_rp": 10026, "ending_rp": 10053, "rp_change": 27, "ranked": True}


def test_chain_expired_falls_back_to_frozen():
    # An anchor older than CHAIN_MAX_AGE (overnight gap, untracked play likely)
    # must not be trusted — the frozen baseline is used instead.
    pending = [rec("A", "yzRobo", 500, 10000, hint=True)]
    observations = {"yzRobo": [obs(9900, 500)] + flat_series(10000, 500, 7)}
    chain = {"yzRobo": {"end": 400, "ts": 100}}   # ancient
    res = resolve_rp(pending, observations, now=10500, chain=chain, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 500, "ending_rp": 500, "rp_change": 0, "ranked": True}
    assert not any("baseline corrected" in n for n in res.notes)


def test_chain_invalidated_by_untracked_play_in_gap():
    # Movement observed in the gap well before the match (an untracked solo game)
    # means the chain can't vouch for this match's baseline — frozen wins, so the
    # untracked game's delta is NOT misattributed to this match.
    pending = [rec("A", "yzRobo", 1040, 3000, hint=True)]
    observations = {"yzRobo": [obs(150, 1000), obs(1200, 1040), obs(2900, 1040)]
                    + flat_series(3000, 1040, 7)}
    chain = {"yzRobo": {"end": 1000, "ts": 160}}
    res = resolve_rp(pending, observations, now=3600, chain=chain, **DEFAULTS)
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 1040, "ending_rp": 1040, "rp_change": 0, "ranked": True}
    assert not any("baseline corrected" in n for n in res.notes)


def test_chain_invalidated_by_reset_in_gap():
    pending = [rec("A", "yzRobo", 120, 3000, hint=True)]
    observations = {"yzRobo": [obs(2500, 120, reset=True), obs(2900, 120)]
                    + flat_series(3000, 120, 7)}
    chain = {"yzRobo": {"end": 1000, "ts": 2400}}
    res = resolve_rp(pending, observations, now=3600, chain=chain, **DEFAULTS)
    # Frozen baseline (120) used; zero-confirms against it, never a -880 "delta".
    assert patch_for(res, "A", "yzRobo") == {"starting_rp": 120, "ending_rp": 120, "rp_change": 0, "ranked": True}


def test_chain_updates_emitted_on_resolutions():
    pending = [rec("A", "yzRobo", 1000, 1000, hint=None)]
    observations = {"yzRobo": [obs(1000, 1000), obs(1200, 1030)]}
    res = resolve_rp(pending, observations, now=1300, **DEFAULTS)
    assert res.chain_updates["yzRobo"] == {"end": 1030, "ts": 1200}
    # Zero path anchors at the confirmed-flat value.
    pending = [rec("Z", "SaWaii", 800, 1000, hint=True)]
    observations = {"SaWaii": [obs(900, 800)] + flat_series(1000, 800, 7)}
    res = resolve_rp(pending, observations, now=1500, **DEFAULTS)
    assert res.chain_updates["SaWaii"]["end"] == 800


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
