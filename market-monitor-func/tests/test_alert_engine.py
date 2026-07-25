from shared.alert_engine import check_level_crossings, check_positions, check_vix_move

LEVELS = [{"label": "Resistance R1", "price": 24900}]


def test_level_first_observation_does_not_alert(state_store):
    alerts = check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_level_cross_above_alerts(state_store):
    check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    alerts = check_level_crossings("NIFTY", 24910, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert len(alerts) == 1
    assert "crossed above" in alerts[0].detail


def test_level_no_repeat_alert_while_above(state_store):
    check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    check_level_crossings("NIFTY", 24910, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    alerts = check_level_crossings("NIFTY", 24950, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_level_hysteresis_prevents_flap_near_boundary(state_store):
    check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    # price nudges just 1 point above the level - inside the 10-point band, should not flip
    alerts = check_level_crossings("NIFTY", 24901, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_level_cross_back_below_alerts_again(state_store):
    # cooldown_minutes=0 here: this test is about direction-flip detection,
    # not cooldown gating (which is exercised elsewhere) - back-to-back
    # crosses in the same test run happen faster than any real cooldown.
    check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=0, state_store=state_store)
    check_level_crossings("NIFTY", 24950, LEVELS, hysteresis_points=10, cooldown_minutes=0, state_store=state_store)
    alerts = check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=0, state_store=state_store)
    assert len(alerts) == 1
    assert "crossed below" in alerts[0].detail


def test_level_cross_suppressed_within_cooldown(state_store):
    check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    check_level_crossings("NIFTY", 24950, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    # immediately flips back - well within the 15 minute cooldown, so no second alert
    alerts = check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_vix_first_reading_sets_baseline_no_alert(state_store):
    alerts = check_vix_move(14.0, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_vix_move_beyond_threshold_alerts_once(state_store):
    check_vix_move(14.0, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store)
    alerts = check_vix_move(15.0, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store)  # +7.1%
    assert len(alerts) == 1
    alerts_again = check_vix_move(15.2, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store)
    assert alerts_again == []  # still tripped, no repeat


def test_vix_rearms_after_falling_back_under_threshold(state_store):
    # cooldown_minutes=0: isolating the trip/re-arm state machine from
    # cooldown gating, which is exercised in test_level_cross_suppressed_within_cooldown.
    check_vix_move(14.0, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store)
    check_vix_move(15.0, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store)  # trips
    check_vix_move(14.1, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store)  # re-arms
    alerts = check_vix_move(15.0, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store)  # trips again
    assert len(alerts) == 1


def test_position_pnl_absolute_threshold(state_store):
    positions_config = {
        "pnl_absolute_enabled": True,
        "pnl_absolute_threshold_rupees": 5000,
        "pnl_percentage_enabled": False,
        "ltp_move_enabled": False,
    }
    positions = [{"tradingsymbol": "NIFTY24800CE", "quantity": 50, "average_price": 120, "last_price": 220, "pnl": 5000}]
    alerts = check_positions(positions, positions_config, cooldown_minutes=15, state_store=state_store)
    assert len(alerts) == 1
    assert "NIFTY24800CE" in alerts[0].title


def test_position_below_threshold_no_alert(state_store):
    positions_config = {
        "pnl_absolute_enabled": True,
        "pnl_absolute_threshold_rupees": 5000,
        "pnl_percentage_enabled": False,
        "ltp_move_enabled": False,
    }
    positions = [{"tradingsymbol": "NIFTY24800CE", "quantity": 50, "average_price": 120, "last_price": 140, "pnl": 1000}]
    alerts = check_positions(positions, positions_config, cooldown_minutes=15, state_store=state_store)
    assert alerts == []
