import time
import copy
import random
from models import NetworkState, StationNode, TrackSegment, TrainState, TrackStatus, WeatherCondition
from graph import RailwayGraph

class DigitalTwin:
    def __init__(self, graph: RailwayGraph):
        self.graph = graph
        stations = {}
        for n, data in graph.graph.nodes(data=True):
            stations[n] = StationNode(
                station_id=n,
                name=data.get("name", n),
                congestion_level=0.2,
                active_signals={}
            )
            
        tracks = {}
        for u, v, data in graph.graph.edges(data=True):
            t_id = data["track_id"]
            if t_id not in tracks:
                tracks[t_id] = TrackSegment(
                    track_id=t_id,
                    source=u,
                    destination=v,
                    health=data["health"],
                    status=data["status"],
                    length_km=data["length_km"],
                    max_speed_kmh=data["max_speed_kmh"]
                )
                
        self.state = NetworkState(
            weather={},
            tracks=tracks,
            trains={},
            stations=stations,
            timestamp=time.time()
        )

    def get_state(self):
        dump = self.state.model_dump()
        
        for st in dump["stations"].values():
            st["active_signals"] = {k: v.value if hasattr(v, "value") else v for k, v in st["active_signals"].items()}
        for tr in dump["tracks"].values():
            if hasattr(tr["status"], "value"):
                tr["status"] = tr["status"].value
        for k, w in dump["weather"].items():
            if hasattr(w, "value"):
                dump["weather"][k] = w.value
                
        return dump

    def copy(self):
        return copy.deepcopy(self)

    def close_track(self, track_id):
        if track_id in self.state.tracks:
            self.state.tracks[track_id].status = TrackStatus.CLOSED
            self.state.tracks[track_id].health = 0.0
        self.graph.close_track(track_id)

    def find_route(self, source, destination):
        return self.graph.find_route(source, destination)

    def reroute_train(self, train_id, route):
        if train_id in self.state.trains:
            self.state.trains[train_id].route = route
            if route:
                self.state.trains[train_id].current_station = route[0]

    def apply_action(self, action_string):
        if action_string.startswith("close_track_"):
            parts = action_string.split("close_track_")
            if len(parts) == 2:
                self.close_track(parts[1])
            else:
                raise ValueError(f"Unrecognised action string: {action_string}")
        elif action_string.startswith("reroute_via_route_"):
            if not self.state.trains:
                return
            train_id = next(iter(self.state.trains.keys()))
            route_suffix = action_string.split("reroute_via_route_")[1]
            self.reroute_train(train_id, [route_suffix])
        else:
            raise ValueError(f"Unrecognised action string: {action_string}")

    def seed_trains(self, n):
        random.seed(42)
        nodes = list(self.graph.graph.nodes())
        if len(nodes) < 2:
            return
            
        count = 0
        i = 0
        while count < n:
            src = random.choice(nodes)
            dst = random.choice(nodes)
            if src != dst:
                try:
                    route = self.find_route(src, dst)
                    if route:
                        t_id = f"TR{count:02d}"
                        self.state.trains[t_id] = TrainState(
                            train_id=t_id,
                            current_station=route[0],
                            route=route,
                            speed_kmh=80.0,
                            passengers=200 + (i * 37 % 300),
                            delayed_minutes=0.0
                        )
                        count += 1
                except ValueError:
                    pass
            i += 1

    def calculate_delay(self):
        total = 0.0
        for tr in self.state.trains.values():
            total += tr.delayed_minutes
            for st_id in tr.route:
                if st_id in self.state.stations:
                    total += 5.0 * self.state.stations[st_id].congestion_level
        return total

    def calculate_risk(self):
        total = 0.0
        for tr in self.state.tracks.values():
            w = 0.5
            if tr.status == TrackStatus.CLOSED:
                w = 2.0
            elif tr.status == TrackStatus.DEGRADED:
                w = 1.5
            risk = (1.0 - tr.health) * w
            
            weather_val = self.state.weather.get(tr.track_id)
            if weather_val == WeatherCondition.STORM:
                risk += 0.3
            elif weather_val == WeatherCondition.RAIN:
                risk += 0.1
            elif weather_val == WeatherCondition.FOG:
                risk += 0.15
                
            total += risk
        return total
