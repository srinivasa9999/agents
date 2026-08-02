from shared.indicators import ema, rsi, vwap


def test_ema_seeds_with_sma_then_recurs():
    values = [10, 12, 14, 16, 18, 20]
    result = ema(values, period=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == (10 + 12 + 14) / 3  # seeded with SMA of first 3
    multiplier = 2 / 4
    expected_3 = (16 - result[2]) * multiplier + result[2]
    assert result[3] == expected_3


def test_ema_not_enough_data_returns_all_none():
    assert ema([1, 2], period=5) == [None, None]


def test_ema_rejects_invalid_period():
    import pytest
    with pytest.raises(ValueError):
        ema([1, 2, 3], period=0)


def test_rsi_all_gains_is_100():
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
    result = rsi(closes, period=14)
    assert result[14] == 100.0


def test_rsi_all_losses_is_0():
    closes = list(reversed([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]))
    result = rsi(closes, period=14)
    assert result[14] == 0.0


def test_rsi_flat_series_is_neutral():
    closes = [10.0] * 20
    result = rsi(closes, period=14)
    # no gains and no losses -> avg_loss is 0 but avg_gain is also 0 -> neutral 50
    assert result[14] == 50.0


def test_rsi_not_enough_data_returns_all_none():
    assert rsi([1, 2, 3], period=14) == [None, None, None]


def test_vwap_basic_weighted_average():
    highs = [10, 12]
    lows = [8, 10]
    closes = [9, 11]
    volumes = [100, 300]
    result = vwap(highs, lows, closes, volumes)
    tp0 = (10 + 8 + 9) / 3
    tp1 = (12 + 10 + 11) / 3
    assert result[0] == tp0
    expected_1 = (tp0 * 100 + tp1 * 300) / 400
    assert result[1] == expected_1


def test_vwap_zero_volume_is_none():
    result = vwap([10], [8], [9], [0])
    assert result[0] is None


def test_vwap_rejects_mismatched_lengths():
    import pytest
    with pytest.raises(ValueError):
        vwap([10, 11], [8], [9, 9], [100, 100])
