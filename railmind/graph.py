import networkx as nx
from .models import TrackStatus

class RailwayGraph:
    def __init__(self):
        self.graph = nx.DiGraph()

    def add_station(self, station_id, name, **kwargs):
        self.graph.add_node(station_id, name=name, **kwargs)

    def add_track(self, track_id, source, destination, health, length_km, max_speed_kmh, status):
        attrs = {
            "track_id": track_id,
            "health": health,
            "length_km": length_km,
            "max_speed_kmh": max_speed_kmh,
            "status": status
        }
        self.graph.add_edge(source, destination, **attrs)
        self.graph.add_edge(destination, source, **attrs)

    def close_track(self, track_id):
        for u, v, data in self.graph.edges(data=True):
            if data.get("track_id") == track_id:
                self.graph.edges[u, v]["status"] = TrackStatus.CLOSED
                self.graph.edges[u, v]["health"] = 0.0

    def find_route(self, source, destination):
        if source == destination:
            return []
            
        def weight_func(u, v, d):
            if d.get("status") in (TrackStatus.CLOSED, TrackStatus.DEGRADED):
                return float("inf")
            return d.get("length_km", 0.0) / d.get("max_speed_kmh", 1.0)
            
        try:
            return nx.shortest_path(self.graph, source=source, target=destination, weight=weight_func)
        except nx.NetworkXNoPath:
            raise ValueError(f"No path found between {source} and {destination}")

    def get_graph_snapshot(self):
        nodes = []
        for n, data in self.graph.nodes(data=True):
            node_data = {"id": n}
            node_data.update(data)
            nodes.append(node_data)
            
        edges = []
        for u, v, data in self.graph.edges(data=True):
            edge_data = {"source": u, "target": v}
            for k, val in data.items():
                if hasattr(val, "value"):
                    edge_data[k] = val.value
                else:
                    edge_data[k] = val
            edges.append(edge_data)
            
        return {"nodes": nodes, "edges": edges}

def build_network_from_data(stations, tracks):
    graph = RailwayGraph()
    for st in stations:
        graph.add_station(
            station_id=st["station_id"],
            name=st["name"],
            lat=st["lat"],
            lon=st["lon"]
        )
    for tr in tracks:
        status = TrackStatus.DEGRADED if tr["health"] < 0.4 else TrackStatus.OPEN
        graph.add_track(
            track_id=tr["track_id"],
            source=tr["source"],
            destination=tr["destination"],
            health=tr["health"],
            length_km=tr["length_km"],
            max_speed_kmh=tr["max_speed_kmh"],
            status=status
        )
    return graph
