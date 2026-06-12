from copy import deepcopy

import networkx as nx


class DigitalTwin:
    def __init__(self, weather, tracks, trains, railway_graph):
        self.weather = weather
        self.tracks = tracks
        self.trains = trains
        self.graph = railway_graph

    def copy(self):
        return DigitalTwin(
            deepcopy(self.weather),
            deepcopy(self.tracks),
            deepcopy(self.trains),
            self.graph.copy(),
        )

    def get_state(self):
        return {
            "weather": deepcopy(self.weather),
            "tracks": deepcopy(self.tracks),
            "trains": deepcopy(self.trains),
            "graph_edges": list(self.graph.edges()),
        }

    def close_track(self, track_id):
        if track_id not in self.tracks:
            return

        track = self.tracks[track_id]
        track["closed"] = True

        if self.graph.has_edge(track["source"], track["destination"]):
            self.graph.remove_edge(track["source"], track["destination"])

    def find_route(self, source, destination):
        try:
            return nx.shortest_path(self.graph, source, destination)
        except nx.NetworkXNoPath:
            return []

    def reroute_train(self, train_id, route):
        if train_id in self.trains:
            self.trains[train_id]["route"] = route

    def apply_action(self, action):
        train_id = "T1"
        train = self.trains[train_id]

        if isinstance(action, dict):
            if action["type"] == "set_route":
                self.reroute_train(action["train_id"], action["route"])
            return

        if action in ("close", "close_track"):
            self.close_track(self._weakest_track_id())

        elif action == "reroute_via_A":
            route = self._route_via(train["source"], train["destination"], "A")
            self.reroute_train(train_id, route)

        elif action == "reroute_via_B":
            route = self._route_via(train["source"], train["destination"], "B")
            self.reroute_train(train_id, route)

        elif action == "keep_current_route":
            route = self.find_route(train["source"], train["destination"])
            self.reroute_train(train_id, route)

        elif action in ("reduce_speed", "restrict_speed", "speed_restriction"):
            train["speed_restricted"] = True

    def calculate_delay(self):
        train = self.trains["T1"]
        route = train["route"]
        delay = 0

        if not route:
            return 999

        if len(route) > 3:
            delay += (len(route) - 3) * 10

        if train.get("speed_restricted"):
            delay += 10

        return delay

    def calculate_risk(self):
        train = self.trains["T1"]
        route = train["route"]

        if not route:
            return 1.0

        rainfall = self.weather["rainfall"]
        risk = 0.1

        if rainfall > 80:
            risk += 0.35
        elif rainfall > 50:
            risk += 0.2

        route_track_health = self._route_track_health(route)
        if route_track_health < 0.4:
            risk += 0.4
        elif route_track_health < 0.7:
            risk += 0.2

        if train.get("speed_restricted"):
            risk -= 0.2

        return min(max(risk, 0), 1)

    def _weakest_track_id(self):
        return min(self.tracks, key=lambda track_id: self.tracks[track_id]["health"])

    def _route_via(self, source, destination, via):
        first_leg = self.find_route(source, via)
        second_leg = self.find_route(via, destination)

        if not first_leg or not second_leg:
            return self.find_route(source, destination)

        return first_leg + second_leg[1:]

    def _route_track_health(self, route):
        health_values = []

        for source, destination in zip(route, route[1:]):
            for track in self.tracks.values():
                same_direction = (
                    track["source"] == source
                    and track["destination"] == destination
                )
                reverse_direction = (
                    track["source"] == destination
                    and track["destination"] == source
                )

                if (same_direction or reverse_direction) and not track["closed"]:
                    health_values.append(track["health"])

        if not health_values:
            return 0

        return min(health_values)
