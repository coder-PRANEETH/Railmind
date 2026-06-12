class DemoTrackModel:
    def predict(self, features):
        track_health = features["track_health"]
        track_age = features["track_age"]
        maintenance_days = features["maintenance_days"]
        weather_risk = features["weather_risk"]

        if track_health < 0.35 or weather_risk > 0.8:
            return "close"

        if (
            track_health < 0.7
            or track_age > 15
            or maintenance_days > 30
            or weather_risk > 0.5
        ):
            return "restrict_speed"

        return "safe"


demo_track_model = DemoTrackModel()
