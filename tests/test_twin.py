import random

import pytest

from railmind.data_loader import load_bundled_network
from railmind.graph import build_network_from_data
from railmind.models import TrackStatus
from railmind.simulator import FutureTimelineSimulator
from railmind.twin import DigitalTwin


@pytest.fixture()
def twin():
    stations, tracks = load_bundled_network()
    graph = build_network_from_data(stations, tracks)
    t = DigitalTwin(graph)
    t.seed_trains(5)
    return t


def _two_station_twin(health=0.9):
    stations = [
        {"station_id": "A", "name": "Alpha", "lat": 0.0, "lon": 0.0},
        {"station_id": "B", "name": "Beta", "lat": 1.0, "lon": 1.0},
    ]
    tracks = [{
        "track_id": "TX",
        "source": "A",
        "destination": "B",
        "health": health,
        "length_km": 100.0,
        "max_speed_kmh": 100.0,
    }]
    return DigitalTwin(build_network_from_data(stations, tracks))


def _nested_id_twin():
    # Station ids where one id ("A_B") is the underscore-join of two others,
    # as live-OSM naming readily produces. Edges: A-B and B-A_B.
    stations = [
        {"station_id": "A", "name": "A", "lat": 0.0, "lon": 0.0},
        {"station_id": "B", "name": "B", "lat": 1.0, "lon": 0.0},
        {"station_id": "A_B", "name": "A B", "lat": 2.0, "lon": 0.0},
    ]
    tracks = [
        {"track_id": "T1", "source": "A", "destination": "B",
         "health": 0.9, "length_km": 50.0, "max_speed_kmh": 100.0},
        {"track_id": "T2", "source": "B", "destination": "A_B",
         "health": 0.9, "length_km": 50.0, "max_speed_kmh": 100.0},
    ]
    t = DigitalTwin(build_network_from_data(stations, tracks))
    t.seed_trains(1)
    return t


def test_seed_trains_deterministic(twin):
    stations, tracks = load_bundled_network()
    other = DigitalTwin(build_network_from_data(stations, tracks))
    other.seed_trains(5)
    assert len(twin.state.trains) == 5
    for tid, train in twin.state.trains.items():
        assert other.state.trains[tid].route == train.route


def test_close_track_updates_state_and_graph(twin):
    risk_before = twin.calculate_risk()
    twin.close_track("T23")
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED
    assert twin.state.tracks["T23"].health == 0.0
    assert twin.calculate_risk() > risk_before


def test_find_route_avoids_closed_track(twin):
    # T23 is the only direct Nagpur-Raipur link; closing it forces a detour
    direct = twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT")
    assert direct == ["NAGPUR_JUNCT", "RAIPUR_JUNCT"]
    twin.close_track("T23")
    detour = twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT")
    assert len(detour) > 2
    for i in range(len(detour) - 1):
        assert {detour[i], detour[i + 1]} != {"NAGPUR_JUNCT", "RAIPUR_JUNCT"}


def test_tick_moves_trains(twin):
    positions_before = {
        tid: (t.route_index, t.progress) for tid, t in twin.state.trains.items()
    }
    for _ in range(3):
        twin.tick()
    moved = any(
        (t.route_index, t.progress) != positions_before[tid]
        for tid, t in twin.state.trains.items()
    )
    assert moved


def test_tick_holds_train_at_closed_track(twin):
    train = next(iter(twin.state.trains.values()))
    # Close the edge directly ahead of the train
    nxt = train.route[train.route_index + 1]
    track = twin._track_between(train.current_station, nxt)
    assert track is not None
    twin.close_track(track.track_id)

    delayed_before = train.delayed_minutes
    twin.tick()
    assert train.held is True
    assert train.delayed_minutes > delayed_before
    assert train.current_station == train.route[train.route_index]


def test_reroute_train_resets_position(twin):
    tid = next(iter(twin.state.trains.keys()))
    new_route = ["NEW_DELHI", "JAIPUR_JUNCT", "BHOPAL_JUNCT"]
    twin.reroute_train(tid, new_route)
    train = twin.state.trains[tid]
    assert train.route == new_route
    assert train.current_station == "NEW_DELHI"
    assert train.route_index == 0 and train.progress == 0.0


def test_reroute_train_rejects_degenerate_route(twin):
    tid = next(iter(twin.state.trains.keys()))
    with pytest.raises(ValueError):
        twin.reroute_train(tid, ["NEW_DELHI"])
    with pytest.raises(ValueError):
        twin.reroute_train(tid, [])


def test_find_route_unknown_station_raises_value_error(twin):
    with pytest.raises(ValueError):
        twin.find_route("DELHI", "MUMBAI")


def test_set_weather_validates_track(twin):
    twin.set_weather("T05", "STORM")
    assert twin.state.weather["T05"].value == "STORM"
    twin.set_weather("T05", "CLEAR")
    assert "T05" not in twin.state.weather
    with pytest.raises(ValueError):
        twin.set_weather("NOPE", "STORM")


def test_maybe_tick_respects_interval(twin):
    assert twin.maybe_tick(min_interval_s=9999) is False
    twin._last_tick = 0.0
    assert twin.maybe_tick(min_interval_s=1) is True


def test_injected_weather_survives_ambient_decay(twin):
    twin.set_weather("T05", "STORM")
    for _ in range(50):
        twin.tick()
    assert twin.state.weather["T05"].value == "STORM"


def test_find_route_raises_when_only_link_closed():
    t = _two_station_twin()
    assert t.find_route("A", "B") == ["A", "B"]
    t.close_track("TX")
    with pytest.raises(ValueError):
        t.find_route("A", "B")


def test_find_route_traverses_degraded_as_last_resort():
    # health < 0.4 marks the track DEGRADED at build time
    t = _two_station_twin(health=0.2)
    assert t.state.tracks["TX"].status == TrackStatus.DEGRADED
    assert t.find_route("A", "B") == ["A", "B"]


def test_seed_trains_leaves_global_random_untouched():
    stations, tracks = load_bundled_network()
    random.seed(123)
    expected = [random.random() for _ in range(5)]
    random.seed(123)
    t = DigitalTwin(build_network_from_data(stations, tracks))
    t.seed_trains(5)
    got = [random.random() for _ in range(5)]
    assert got == expected


def test_apply_reroute_action_string(twin):
    tid = next(iter(twin.state.trains.keys()))
    twin.apply_action(f"reroute_{tid}_via_NEW_DELHI_JAIPUR_JUNCT_BHOPAL_JUNCT")
    train = twin.state.trains[tid]
    assert train.route == ["NEW_DELHI", "JAIPUR_JUNCT", "BHOPAL_JUNCT"]
    assert train.current_station == "NEW_DELHI"
    assert train.route_index == 0 and train.progress == 0.0


def test_apply_reroute_action_rejects_malformed(twin):
    tid = next(iter(twin.state.trains.keys()))
    with pytest.raises(ValueError):
        twin.apply_action("reroute_via_route_A")  # legacy form: no train id
    with pytest.raises(ValueError):
        twin.apply_action(f"reroute_{tid}_via_NEW_DELHI")  # single station
    with pytest.raises(ValueError):
        twin.apply_action(f"reroute_{tid}_via_NOPE_NOWHERE")  # unknown stations
    with pytest.raises(ValueError):
        twin.apply_action("reroute_GHOST_via_NEW_DELHI_JAIPUR_JUNCT")  # unknown train


def test_apply_reroute_action_rejects_non_adjacent_route(twin):
    # Both stations exist but no track joins them: the train must not teleport
    tid = next(iter(twin.state.trains.keys()))
    with pytest.raises(ValueError):
        twin.apply_action(f"reroute_{tid}_via_NEW_DELHI_MUMBAI_CENTR")


def test_reroute_parser_prefers_connected_segmentation():
    # Greedy longest-match alone would read "A_B" as the single nested id and
    # then fail the two-station rule; the connected parse [A, B] must win.
    t = _nested_id_twin()
    tid = next(iter(t.state.trains.keys()))
    t.apply_action(f"reroute_{tid}_via_A_B")
    assert t.state.trains[tid].route == ["A", "B"]


def test_reroute_parser_resolves_genuine_nested_id():
    # Here the nested id is really meant: B connects to A_B, not to [A, B]
    # twice over, so the parse must keep "A_B" whole.
    t = _nested_id_twin()
    tid = next(iter(t.state.trains.keys()))
    t.apply_action(f"reroute_{tid}_via_B_A_B")
    assert t.state.trains[tid].route == ["B", "A_B"]


def test_reroute_parser_is_polynomial_on_adversarial_strings():
    # A clique of nested ids gives exponentially many segmentations; an
    # unreachable tail forces the parser to reject them all. Without
    # failure memoization this hangs for minutes — HTTP callers feed
    # arbitrary strings into this parser, so it must stay fast.
    import itertools
    import time

    names = ["A", "B", "C", "A_B", "B_C"]
    stations = [
        {"station_id": n, "name": n, "lat": float(i), "lon": 0.0}
        for i, n in enumerate(names)
    ]
    stations += [
        {"station_id": "LONELY", "name": "L", "lat": 9.0, "lon": 9.0},
        {"station_id": "OUT", "name": "O", "lat": 9.5, "lon": 9.0},
    ]
    tracks = []
    for k, (u, v) in enumerate(itertools.combinations(names, 2)):
        tracks.append({"track_id": f"T{k}", "source": u, "destination": v,
                       "health": 0.9, "length_km": 50.0, "max_speed_kmh": 100.0})
    tracks.append({"track_id": "TL", "source": "LONELY", "destination": "OUT",
                   "health": 0.9, "length_km": 50.0, "max_speed_kmh": 100.0})
    t = DigitalTwin(build_network_from_data(stations, tracks))
    t.seed_trains(1)
    tid = next(iter(t.state.trains.keys()))

    route = "_".join(["A_B_C"] * 40) + "_LONELY"
    start = time.perf_counter()
    with pytest.raises(ValueError):
        t.apply_action(f"reroute_{tid}_via_{route}")
    assert time.perf_counter() - start < 2.0


def test_close_track_unknown_id_raises(twin):
    with pytest.raises(ValueError):
        twin.close_track("NOPE")
    with pytest.raises(ValueError):
        twin.apply_action("close_track_NOPE")


def test_held_trains_do_not_desync_rng_stream(twin):
    # Two clones on the same seed, one holding a train at a closed track:
    # every train must consume one jitter draw per tick regardless, so the
    # ambient randomness stays in lockstep across scenarios.
    a = twin.copy()
    b = twin.copy()
    a._rng = random.Random(7)
    b._rng = random.Random(7)

    train = next(iter(b.state.trains.values()))
    nxt = train.route[train.route_index + 1]
    track = b._track_between(train.current_station, nxt)
    b.close_track(track.track_id)

    for _ in range(10):
        a.tick()
        b.tick()
    assert b.state.trains[train.train_id].held is True
    assert a.state.weather == b.state.weather
    # Sentinel draw: both RNG streams sit at the same position
    assert a._rng.random() == b._rng.random()


def test_simulator_differentiates_closure_from_do_nothing(twin):
    # Close the track directly ahead of a train: the forecast must hold it
    # and accumulate delay, so the score diverges from inaction.
    train = next(iter(twin.state.trains.values()))
    nxt = train.route[train.route_index + 1]
    track = twin._track_between(train.current_station, nxt)
    sim = FutureTimelineSimulator(twin)

    do_nothing = sim.generate_scenario("A", [])
    closure = sim.generate_scenario("B", [f"close_track_{track.track_id}"])
    assert closure.delay_score > do_nothing.delay_score
    assert closure.total_score != do_nothing.total_score

    # Forecasts are deterministic: repeated runs agree exactly
    again = sim.generate_scenario("B", [f"close_track_{track.track_id}"])
    assert again.total_score == closure.total_score


def test_do_nothing_loses_when_acting_is_clearly_better(twin):
    # Strand a train on the live twin, then compare inaction with rerouting
    # it around the closure: acting must win.
    tid, train = next(iter(twin.state.trains.items()))
    nxt = train.route[train.route_index + 1]
    track = twin._track_between(train.current_station, nxt)
    twin.close_track(track.track_id)

    detour = twin.find_route(train.current_station, train.route[-1])
    reroute_action = f"reroute_{tid}_via_" + "_".join(detour)

    sim = FutureTimelineSimulator(twin)
    result = sim.run([
        {"scenario_id": "do_nothing", "actions": []},
        {"scenario_id": "act", "actions": [reroute_action]},
    ])
    assert result.best_scenario == "act"
    assert result.recommended_actions == [reroute_action]


def test_simulator_empty_scenarios_raises(twin):
    sim = FutureTimelineSimulator(twin)
    with pytest.raises(ValueError):
        sim.run([])


def test_unpinned_weather_decays(twin):
    twin.set_weather("T05", "STORM", pin=False)
    assert "T05" not in twin._pinned_weather
    cleared = False
    for _ in range(200):
        twin.tick()
        if "T05" not in twin.state.weather:
            cleared = True
            break
    assert cleared


def test_clearing_weather_unpins_track(twin):
    twin.set_weather("T05", "STORM")
    assert "T05" in twin._pinned_weather
    twin.set_weather("T05", "CLEAR")
    assert "T05" not in twin._pinned_weather
    # Once unpinned, ambient decay may clear any future condition again
    twin.state.weather = {}
    for _ in range(50):
        twin.tick()
    # No assertion on presence — just ensure ticking never resurrects the pin
    assert "T05" not in twin._pinned_weather
