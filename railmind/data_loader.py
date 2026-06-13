import math
import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c

def fetch_osm_railway(area_name):
    if area_name == "India":
        query = f"""
        [out:json][timeout:180];
        area["name"="{area_name}"]->.search_area;
        (
          node["railway"="station"](area.search_area);
        );
        out body;
        """
    else:
        query = f"""
        [out:json][timeout:180];
        area["name"="{area_name}"]->.search_area;
        (
          node["railway"="station"](area.search_area);
          node["railway"="halt"](area.search_area);
          way["railway"="rail"](area.search_area);
        );
        out body;
        >;
        out skel qt;
        """
    response = requests.post(OVERPASS_URL, data=query, headers={"User-Agent": "RailMind/1.0"}, timeout=180)
    response.raise_for_status()
    return response.json()

def parse_osm_data(raw):
    stations = []
    tracks = []
    station_lookup = {}
    
    all_nodes = {el["id"]: el for el in raw.get("elements", []) if el.get("type") == "node"}
    way_node_ids = {n for el in raw.get("elements", []) if el.get("type") == "way" and el.get("tags", {}).get("railway") == "rail" for n in el.get("nodes", [])}

    for el in raw.get("elements", []):
        if el.get("type") == "node" and el.get("tags", {}).get("railway") in ("station", "halt"):
            osm_id = el["id"]
            name = el["tags"].get("name")
            if name:
                station_id = name.upper().replace(" ", "_")[:12]
            else:
                station_id = f"STN_{osm_id}"
            
            station_data = {
                "osm_id": osm_id,
                "station_id": station_id,
                "name": name or station_id,
                "lat": el["lat"],
                "lon": el["lon"]
            }
            stations.append(station_data)
            station_lookup[osm_id] = station_data
            
            dists = []
            for n_id in way_node_ids:
                if n_id in all_nodes:
                    dists.append((haversine(station_data["lat"], station_data["lon"], all_nodes[n_id]["lat"], all_nodes[n_id]["lon"]), n_id))
            if dists:
                min_dist, closest = min(dists)
                if min_dist < 3.0:
                    station_lookup[closest] = station_data

    ways = [el for el in raw.get("elements", []) if el.get("type") == "way" and el.get("tags", {}).get("railway") == "rail"]
    
    if not ways and len(stations) > 0:
        # India dynamic mode: Filter real stations and build MST
        target_cities = ["New Delhi", "Mumbai", "Howrah", "Chennai", "Bengaluru", "Secunderabad", "Ahmedabad", "Jaipur", "Bhopal", "Lucknow", "Patna", "Bhubaneswar", "Guwahati", "Nagpur", "Pune", "Thiruvananthapuram", "Ranchi", "Raipur", "Dehradun", "Chandigarh", "Jammu"]
        filtered_stations = []
        used_cities = set()
        
        for city in target_cities:
            best_match = None
            best_score = -1
            for s in stations:
                name = s.get("name", "")
                if city.lower() in name.lower() and city not in used_cities:
                    score = 0
                    lower_name = name.lower()
                    lower_city = city.lower()
                    
                    if lower_name == lower_city:
                        score += 100
                    elif lower_name == f"{lower_city} junction" or lower_name == f"{lower_city} central":
                        score += 90
                    elif lower_name.startswith(lower_city):
                        score += 50
                        
                    if "Junction" in name or "Central" in name or "Terminus" in name or "Cantt" in name:
                        score += 30
                    if "Nursing" in name or "Metro" in name or "East" in name or "West" in name:
                        score -= 50
                    if len(name) - len(city) < 10:
                        score += 10
                        
                    if score > best_score:
                        best_score = score
                        best_match = s
                        
            if best_match:
                # Clean up the name for the plot if it's too long
                if len(best_match["name"]) > 20:
                    best_match["name"] = city + (" Jn" if "Junction" in best_match["name"] else "")
                filtered_stations.append(best_match)
                used_cities.add(city)
        
        if not filtered_stations:
            filtered_stations = stations[:20]
            
        import networkx as nx
        G = nx.Graph()
        for s in filtered_stations:
            G.add_node(s["station_id"])
        
        for s1 in filtered_stations:
            for s2 in filtered_stations:
                if s1["station_id"] != s2["station_id"]:
                    d = haversine(s1["lat"], s1["lon"], s2["lat"], s2["lon"])
                    G.add_edge(s1["station_id"], s2["station_id"], weight=d)
                    
        mst = nx.minimum_spanning_tree(G)
        
        edges_to_add = []
        for n in mst.nodes():
            neighbors = sorted([(G[n][nbr]['weight'], nbr) for nbr in G.neighbors(n)])
            for w, nbr in neighbors[1:3]:
                if not mst.has_edge(n, nbr) and w < 1200:
                    edges_to_add.append((n, nbr, w))
                    break
                    
        for u, v, w in edges_to_add:
            mst.add_edge(u, v, weight=w)
            
        track_count = 1
        for u, v, data_dict in mst.edges(data=True):
            dist = data_dict['weight']
            health = round(0.55 + (abs(hash(frozenset({u, v}))) % 45) / 100, 2)
            tracks.append({
                "track_id": f"T{track_count:02d}",
                "source": u,
                "destination": v,
                "length_km": dist,
                "max_speed_kmh": 130.0,
                "health": health
            })
            track_count += 1
            
        return filtered_stations, tracks

    seen_pairs = set()
    track_count = 1
    
    for el in raw.get("elements", []):
        if el.get("type") == "way" and el.get("tags", {}).get("railway") == "rail":
            refs = [r for r in el.get("nodes", []) if r in station_lookup]
            if len(refs) < 2:
                continue
            
            tags = el.get("tags", {})
            maxspeed_str = tags.get("maxspeed", "120")
            maxspeed_str = maxspeed_str.replace(" mph", "")
            try:
                max_speed_kmh = float(maxspeed_str)
            except ValueError:
                max_speed_kmh = 120.0
            
            max_speed_kmh = min(max_speed_kmh, 200.0)
            
            for i in range(len(refs) - 1):
                src_osm_id = refs[i]
                dst_osm_id = refs[i+1]
                src_station = station_lookup[src_osm_id]
                dst_station = station_lookup[dst_osm_id]
                
                dist = haversine(src_station["lat"], src_station["lon"], dst_station["lat"], dst_station["lon"])
                if dist < 0.1:
                    continue
                
                pair = frozenset({src_osm_id, dst_osm_id})
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                
                health = round(0.55 + (abs(hash(pair)) % 45) / 100, 2)
                track_id = f"T{track_count:02d}"
                track_count += 1
                
                tracks.append({
                    "track_id": track_id,
                    "source": src_station["station_id"],
                    "destination": dst_station["station_id"],
                    "length_km": dist,
                    "max_speed_kmh": max_speed_kmh,
                    "health": health
                })
                
    return stations, tracks

def load_railway(area_name):
    raw = fetch_osm_railway(area_name)
    stations, tracks = parse_osm_data(raw)
    if len(stations) < 3 or len(tracks) < 2:
        raise ValueError(f"Insufficient network data for area '{area_name}'")
    return stations, tracks
