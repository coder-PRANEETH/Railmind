from railmind.data_loader import haversine, load_bundled_network, load_railway, stable_track_health


def test_bundled_network_shape():
    stations, tracks = load_bundled_network()
    assert len(stations) == 21
    assert len(tracks) == 33
    station_ids = {s["station_id"] for s in stations}
    for tr in tracks:
        assert tr["source"] in station_ids
        assert tr["destination"] in station_ids
        assert tr["length_km"] > 0
        assert 0.0 <= tr["health"] <= 1.0


def test_load_railway_defaults_to_bundled_offline():
    stations, tracks = load_railway("India", live=False)
    assert len(stations) == 21 and len(tracks) == 33


def test_stable_health_is_deterministic_and_symmetric():
    a = stable_track_health("NEW_DELHI", "JAIPUR_JUNCT")
    b = stable_track_health("JAIPUR_JUNCT", "NEW_DELHI")
    assert a == b
    assert a == stable_track_health("NEW_DELHI", "JAIPUR_JUNCT")
    assert 0.55 <= a <= 0.99


def test_haversine_known_distance():
    # New Delhi to Mumbai is roughly 1150 km great-circle
    d = haversine(28.6427, 77.2207, 18.9696, 72.8195)
    assert 1100 < d < 1220
