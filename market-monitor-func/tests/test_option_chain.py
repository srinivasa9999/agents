from shared.option_chain import compute_pcr, select_atm_strikes, summarize_chain


def test_select_atm_strikes_picks_nearest_and_symmetric_window():
    strikes = [100, 200, 300, 400, 500, 600, 700]
    selected = select_atm_strikes(strikes, spot_price=410, strikes_each_side=2)
    # ATM is 400 (nearest to 410); +/-2 strikes -> 200,300,400,500,600
    assert selected == [200, 300, 400, 500, 600]


def test_select_atm_strikes_clamps_at_edges():
    strikes = [100, 200, 300]
    selected = select_atm_strikes(strikes, spot_price=100, strikes_each_side=5)
    assert selected == [100, 200, 300]


def test_select_atm_strikes_dedupes_and_sorts_input():
    strikes = [300, 100, 200, 100, 300]
    selected = select_atm_strikes(strikes, spot_price=100, strikes_each_side=1)
    assert selected == [100, 200]


def test_select_atm_strikes_empty_input():
    assert select_atm_strikes([], spot_price=100, strikes_each_side=3) == []


def test_compute_pcr_basic():
    assert compute_pcr(total_put_oi=140, total_call_oi=100) == 1.4


def test_compute_pcr_zero_call_oi_returns_none():
    assert compute_pcr(total_put_oi=50, total_call_oi=0) is None


def test_summarize_chain_totals_and_max_strikes():
    chain_quotes = [
        {"strike": 24500, "ce_oi": 1000, "pe_oi": 5000},
        {"strike": 24600, "ce_oi": 9000, "pe_oi": 2000},
        {"strike": 24700, "ce_oi": 3000, "pe_oi": 8000},
    ]
    summary = summarize_chain(chain_quotes)
    assert summary["total_call_oi"] == 13000
    assert summary["total_put_oi"] == 15000
    assert summary["pcr"] == 15000 / 13000
    assert summary["max_call_oi_strike"] == 24600
    assert summary["max_call_oi"] == 9000
    assert summary["max_put_oi_strike"] == 24700
    assert summary["max_put_oi"] == 8000


def test_summarize_chain_empty_list():
    summary = summarize_chain([])
    assert summary["total_call_oi"] == 0
    assert summary["total_put_oi"] == 0
    assert summary["pcr"] is None
    assert summary["max_call_oi_strike"] is None
    assert summary["max_put_oi_strike"] is None
