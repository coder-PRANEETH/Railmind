from models import ScenarioScore, SimulationResult
from twin import DigitalTwin

class FutureTimelineSimulator:
    def __init__(self, twin: DigitalTwin):
        self.base_twin = twin

    def generate_scenario(self, scenario_id, actions):
        future = self.base_twin.copy()
        for action in actions:
            future.apply_action(action)
            
        delay_score = future.calculate_delay()
        risk_score = future.calculate_risk()
        
        stations = list(future.state.stations.values())
        if stations:
            congestion_score = sum(s.congestion_level for s in stations) / len(stations)
        else:
            congestion_score = 0.0
            
        total_passengers = sum(t.passengers for t in future.state.trains.values())
        passenger_impact_score = total_passengers / 1000.0
        
        return ScenarioScore(
            scenario_id=scenario_id,
            delay_score=delay_score,
            risk_score=risk_score,
            congestion_score=congestion_score,
            passenger_impact_score=passenger_impact_score
        )

    def run(self, scenarios_config):
        scenarios = []
        best_scenario = None
        best_score = float("inf")
        max_score = 0.0
        recommended_actions = []
        
        for config in scenarios_config:
            score = self.generate_scenario(config["scenario_id"], config["actions"])
            scenarios.append(score)
            
            if score.total_score < best_score:
                best_score = score.total_score
                best_scenario = score.scenario_id
                recommended_actions = config["actions"]
                
            if score.total_score > max_score:
                max_score = score.total_score
                
        confidence = 0.0
        if max_score > 0:
            confidence = max(0.0, min(1.0, 1.0 - (best_score / max_score)))
            
        return SimulationResult(
            best_scenario=best_scenario,
            scenarios=scenarios,
            recommended_actions=recommended_actions,
            confidence=confidence
        )

def default_scenarios(twin: DigitalTwin):
    tracks = list(twin.state.tracks.values())
    tracks.sort(key=lambda t: t.health)
    
    actions_c = []
    actions_b = []
    
    if len(tracks) >= 1:
        actions_b.append(f"close_track_{tracks[0].track_id}")
        actions_c.append(f"close_track_{tracks[0].track_id}")
    if len(tracks) >= 2:
        actions_c.append(f"close_track_{tracks[1].track_id}")
        
    return [
        {"scenario_id": "A", "actions": []},
        {"scenario_id": "B", "actions": actions_b},
        {"scenario_id": "C", "actions": actions_c}
    ]
