from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, model_validator

class TrackStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    DEGRADED = "DEGRADED"
    # Transitional states driven by field work: a section being taken out of
    # service, and one a crew is actively rebuilding. Both hold traffic.
    CLOSING = "CLOSING"
    UNDER_REPAIR = "UNDER_REPAIR"

# Trains cannot traverse these; routing hides them exactly like a closure.
IMPASSABLE_TRACK_STATUSES = frozenset({
    TrackStatus.CLOSED,
    TrackStatus.CLOSING,
    TrackStatus.UNDER_REPAIR,
})

class SignalStatus(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"

class WeatherCondition(str, Enum):
    CLEAR = "CLEAR"
    RAIN = "RAIN"
    STORM = "STORM"
    FOG = "FOG"

class TrackSegment(BaseModel):
    track_id: str
    source: str
    destination: str
    health: float
    status: TrackStatus
    length_km: float
    max_speed_kmh: float
    # Temporary operational limit posted by a SPEED_RESTRICT task; trains run
    # at min(own speed, line speed, restriction) while it is in force.
    speed_restriction_kmh: Optional[float] = None

class TrainState(BaseModel):
    train_id: str
    current_station: str
    route: List[str]
    speed_kmh: float
    passengers: int
    delayed_minutes: float
    route_index: int = 0        # position of current_station within route
    progress: float = 0.0       # 0..1 along the edge to the next station
    held: bool = False          # true while blocked by a closed track ahead

class StationNode(BaseModel):
    station_id: str
    name: str
    congestion_level: float
    active_signals: Dict[str, SignalStatus]

class NetworkState(BaseModel):
    weather: Dict[str, WeatherCondition]
    tracks: Dict[str, TrackSegment]
    trains: Dict[str, TrainState]
    stations: Dict[str, StationNode]
    timestamp: float

class ScenarioScore(BaseModel):
    scenario_id: str
    delay_score: float
    risk_score: float
    congestion_score: float
    passenger_impact_score: float
    total_score: float = 0.0

    @model_validator(mode="after")
    def calculate_total(self):
        self.total_score = (
            0.35 * self.delay_score +
            0.30 * self.risk_score +
            0.20 * self.congestion_score +
            0.15 * self.passenger_impact_score
        )
        return self

class SimulationResult(BaseModel):
    best_scenario: str
    scenarios: List[ScenarioScore]
    recommended_actions: List[str]
    confidence: float
