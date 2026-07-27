from shared.pivot_points import compute_classic_pivots, pivots_to_watch_levels


def test_compute_classic_pivots_known_values():
    # H=100, L=90, C=95 -> P=95, range=10
    pivots = compute_classic_pivots(high=100, low=90, close=95)
    assert pivots["P"] == 95
    assert pivots["R1"] == 100
    assert pivots["S1"] == 90
    assert pivots["R2"] == 105
    assert pivots["S2"] == 85
    assert pivots["R3"] == 110
    assert pivots["S3"] == 80


def test_pivots_to_watch_levels_shape_and_order():
    pivots = compute_classic_pivots(high=100, low=90, close=95)
    levels = pivots_to_watch_levels(pivots)

    assert [lvl["label"] for lvl in levels] == [
        "Pivot R3", "Pivot R2", "Pivot R1", "Pivot P", "Pivot S1", "Pivot S2", "Pivot S3",
    ]
    assert all({"label", "price"} == set(lvl.keys()) for lvl in levels)

    r1 = next(lvl for lvl in levels if lvl["label"] == "Pivot R1")
    assert r1["price"] == 100


def test_pivots_to_watch_levels_rounds_prices():
    pivots = compute_classic_pivots(high=100.111, low=90.222, close=95.333)
    levels = pivots_to_watch_levels(pivots)
    for lvl in levels:
        assert lvl["price"] == round(lvl["price"], 2)
