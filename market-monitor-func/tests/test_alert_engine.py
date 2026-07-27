from shared.alert_engine import (
    check_level_crossings,
    check_pcr_extreme,
    check_positions,
    check_top_oi_shift,
    check_vix_move,
)

from .conftest import deliver

LEVELS = [{"label": "Resistance R1", "price": 24900}]


def test_level_first_observation_does_not_alert(state_store):
    alerts = check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_level_cross_above_alerts(state_store):
    deliver(check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store))
    alerts = check_level_crossings("NIFTY", 24910, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert len(alerts) == 1
    assert "crossed above" in alerts[0].detail


def test_level_no_repeat_alert_while_above(state_store):
    deliver(check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store))
    deliver(check_level_crossings("NIFTY", 24910, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store))
    alerts = check_level_crossings("NIFTY", 24950, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_level_hysteresis_prevents_flap_near_boundary(state_store):
    deliver(check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store))
    # price nudges just 1 point above the level - inside the 10-point band, should not flip
    alerts = check_level_crossings("NIFTY", 24901, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_level_cross_back_below_alerts_again(state_store):
    # cooldown_minutes=0 here: this test is about direction-flip detection,
    # not cooldown gating (which is exercised elsewhere) - back-to-back
    # crosses in the same test run happen faster than any real cooldown.
    deliver(check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=0, state_store=state_store))
    deliver(check_level_crossings("NIFTY", 24950, LEVELS, hysteresis_points=10, cooldown_minutes=0, state_store=state_store))
    alerts = check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=0, state_store=state_store)
    assert len(alerts) == 1
    assert "crossed below" in alerts[0].detail


def test_level_cross_suppressed_within_cooldown(state_store):
    deliver(check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store))
    deliver(check_level_crossings("NIFTY", 24950, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store))
    # immediately flips back - well within the 15 minute cooldown, so no second alert
    alerts = check_level_crossings("NIFTY", 24800, LEVELS, hysteresis_points=10, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_vix_first_reading_sets_baseline_no_alert(state_store):
    alerts = check_vix_move(14.0, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_vix_move_beyond_threshold_alerts_once(state_store):
    deliver(check_vix_move(14.0, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store))
    alerts = deliver(check_vix_move(15.0, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store))  # +7.1%
    assert len(alerts) == 1
    alerts_again = check_vix_move(15.2, threshold_pct=5.0, cooldown_minutes=15, state_store=state_store)
    assert alerts_again == []  # still tripped, no repeat


def test_vix_rearms_after_falling_back_under_threshold(state_store):
    # cooldown_minutes=0: isolating the trip/re-arm state machine from
    # cooldown gating, which is exercised in test_level_cross_suppressed_within_cooldown.
    deliver(check_vix_move(14.0, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store))
    deliver(check_vix_move(15.0, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store))  # trips
    deliver(check_vix_move(14.1, threshold_pct=5.0, cooldown_minutes=0, state_store=state_store))  # re-arms
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


def test_pcr_first_reading_extreme_alerts(state_store):
    # Unlike level-cross (which only sets a baseline on first observation),
    # PCR has no "previous side" concept - only tripped/not-tripped - so an
    # extreme value alerts immediately even on the first ever reading.
    alerts = check_pcr_extreme("NIFTY", pcr=1.5, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=15, state_store=state_store)
    assert len(alerts) == 1
    assert "high extreme" in alerts[0].title


def test_pcr_within_band_no_alert(state_store):
    alerts = check_pcr_extreme("NIFTY", pcr=1.0, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_pcr_low_extreme_alerts(state_store):
    alerts = check_pcr_extreme("NIFTY", pcr=0.5, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=15, state_store=state_store)
    assert len(alerts) == 1
    assert "low extreme" in alerts[0].title


def test_pcr_no_repeat_while_still_extreme(state_store):
    deliver(check_pcr_extreme("NIFTY", pcr=1.5, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=15, state_store=state_store))
    alerts = check_pcr_extreme("NIFTY", pcr=1.6, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_pcr_rearms_after_returning_to_band(state_store):
    deliver(check_pcr_extreme("NIFTY", pcr=1.5, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=0, state_store=state_store))
    deliver(check_pcr_extreme("NIFTY", pcr=1.0, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=0, state_store=state_store))
    alerts = check_pcr_extreme("NIFTY", pcr=1.5, low_threshold=0.7, high_threshold=1.3, cooldown_minutes=0, state_store=state_store)
    assert len(alerts) == 1


def test_top_oi_shift_first_observation_sets_baseline(state_store):
    alerts = check_top_oi_shift("NIFTY", "CE", strike=24600, oi=9000, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_top_oi_shift_same_strike_no_alert(state_store):
    deliver(check_top_oi_shift("NIFTY", "CE", strike=24600, oi=9000, cooldown_minutes=15, state_store=state_store))
    alerts = check_top_oi_shift("NIFTY", "CE", strike=24600, oi=9500, cooldown_minutes=15, state_store=state_store)
    assert alerts == []


def test_top_oi_shift_alerts_on_change(state_store):
    deliver(check_top_oi_shift("NIFTY", "CE", strike=24600, oi=9000, cooldown_minutes=15, state_store=state_store))
    alerts = check_top_oi_shift("NIFTY", "CE", strike=24700, oi=9500, cooldown_minutes=15, state_store=state_store)
    assert len(alerts) == 1
    assert "24600" in alerts[0].detail and "24700" in alerts[0].detail


def test_top_oi_shift_suppressed_within_cooldown(state_store):
    deliver(check_top_oi_shift("NIFTY", "CE", strike=24600, oi=9000, cooldown_minutes=15, state_store=state_store))
    deliver(check_top_oi_shift("NIFTY", "CE", strike=24700, oi=9500, cooldown_minutes=15, state_store=state_store))
    alerts = check_top_oi_shift("NIFTY", "CE", strike=24600, oi=9000, cooldown_minutes=15, state_store=state_store)
    assert alerts == []
