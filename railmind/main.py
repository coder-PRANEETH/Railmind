import sys
from data_loader import load_railway
from graph import build_network_from_data
from twin import DigitalTwin
from simulator import FutureTimelineSimulator, default_scenarios

def main():
    stations, tracks = load_railway("India")
    area_loaded = "India"
            
    graph = build_network_from_data(stations, tracks)
    twin = DigitalTwin(graph)
    twin.seed_trains(5)
    
    print("Network Summary")
    print("---------------")
    print(f"Area:           {area_loaded}")
    print(f"Stations:       {len(twin.state.stations)}")
    print(f"Tracks:         {len(twin.state.tracks)}")
    print(f"Trains:         {len(twin.state.trains)}\n")
    
    simulator = FutureTimelineSimulator(twin)
    scenarios_config = default_scenarios(twin)
    result = simulator.run(scenarios_config)
    
    print("Simulation Results")
    print("------------------")
    print(f"Best Scenario:  {result.best_scenario}")
    print(f"Confidence:     {result.confidence:.2f}")
    print("Recommended:    " + ", ".join(result.recommended_actions) if result.recommended_actions else "Recommended:    None")
    print()
    
    for score in result.scenarios:
        print(f"Scenario {score.scenario_id}: Total Score = {score.total_score:.2f} (Delay={score.delay_score:.2f}, Risk={score.risk_score:.2f})")
    print()
    
    if len(twin.state.stations) >= 2:
        s1, s2 = list(twin.state.stations.keys())[:2]
        try:
            route = twin.graph.find_route(s1, s2)
            print(f"Route from {s1} to {s2}:")
            print(" -> ".join(route))
        except ValueError:
            print(f"No route found between {s1} and {s2}.")
    print()
    
    if twin.state.tracks:
        worst_track = min(twin.state.tracks.values(), key=lambda t: t.health)
        t_id = worst_track.track_id
        
        risk_before = twin.calculate_risk()
        twin.close_track(t_id)
        risk_after = twin.calculate_risk()
        
        print("Track Maintenance Demo")
        print("----------------------")
        print(f"Closing track {t_id}")
        print(f"Risk Score Before: {risk_before:.2f}")
        print(f"Risk Score After:  {risk_after:.2f}")

if __name__ == "__main__":
    main()
