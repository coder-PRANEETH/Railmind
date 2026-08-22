import { Train as TrainIcon, Users, MapPin, PauseCircle } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { stationName, type Train } from "@/lib/railmind-data";

type Props = {
  trains: Train[];
  selectedTrain: string | null;
  onSelect: (id: string | null) => void;
  reroutedTrains?: { id: string; newRoute: string[] }[];
  heldTrains?: string[];
};

export function TrainsPanel({
  trains,
  selectedTrain,
  onSelect,
  reroutedTrains = [],
  heldTrains = [],
}: Props) {
  return (
    <Card className="p-0 glass overflow-hidden h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          <TrainIcon className="h-4 w-4 text-info" />
          <div className="text-sm font-semibold">Active Trains</div>
          <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
            FLEET
          </Badge>
        </div>
        <div className="text-[11px] text-muted-foreground font-mono-mc">{trains.length} trains</div>
      </div>
      <ScrollArea className="h-[460px]">
        <div className="p-3 space-y-2">
          {trains.map((t) => {
            const rerouted = reroutedTrains.find((r) => r.id === t.id);
            const held = heldTrains.includes(t.id);
            const route = rerouted?.newRoute ?? t.route;
            const isSelected = selectedTrain === t.id;
            return (
              <button
                key={t.id}
                onClick={() => onSelect(isSelected ? null : t.id)}
                className={`w-full text-left rounded-lg p-3 glass transition-all hover:border-info/60 ${
                  isSelected ? "glow-blue border-info" : ""
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div
                      className={`grid h-7 w-7 place-items-center rounded-md ${
                        held ? "bg-danger/20" : rerouted ? "bg-info/20" : "bg-success/15"
                      }`}
                    >
                      {held ? (
                        <PauseCircle className="h-3.5 w-3.5 text-danger" />
                      ) : (
                        <TrainIcon
                          className={`h-3.5 w-3.5 ${rerouted ? "text-info" : "text-success"}`}
                        />
                      )}
                    </div>
                    <div className="font-mono-mc text-sm font-semibold">{t.id}</div>
                  </div>
                  {held ? (
                    <Badge className="bg-danger/20 text-danger border-danger/40 font-mono-mc text-[10px]">
                      HELD
                    </Badge>
                  ) : rerouted ? (
                    <Badge className="bg-info/20 text-info border-info/40 font-mono-mc text-[10px]">
                      REROUTED
                    </Badge>
                  ) : (
                    <Badge variant="outline" className="font-mono-mc text-[10px]">
                      ON-ROUTE
                    </Badge>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-1.5 text-[11px] text-muted-foreground font-mono-mc">
                  <MapPin className="h-3 w-3" />
                  <span className="truncate">{route.map(stationName).join(" → ")}</span>
                </div>
                <div className="mt-1.5 flex items-center justify-between text-[11px] font-mono-mc">
                  <span className="flex items-center gap-1 text-muted-foreground">
                    <Users className="h-3 w-3" /> {t.passengers}
                  </span>
                  <span className="text-muted-foreground">{route.length} stops</span>
                </div>
              </button>
            );
          })}
        </div>
      </ScrollArea>
    </Card>
  );
}

export function getTrainRoute(
  trainId: string | null,
  trains: Train[],
  reroutedTrains: { id: string; newRoute: string[] }[] = [],
): string[] | null {
  if (!trainId) return null;
  const r = reroutedTrains.find((x) => x.id === trainId);
  if (r) return r.newRoute;
  const t = trains.find((x) => x.id === trainId);
  return t?.route ?? null;
}
