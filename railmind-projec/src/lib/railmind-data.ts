// Canonical RailMind network — mirrors railmind/data/india_network.json in the
// Python backend, so station/track IDs line up across the whole stack.

export type Station = { id: string; name: string; lat: number; lon: number; x: number; y: number };
export type Track = {
  id: string;
  from: string;
  to: string;
  length_km: number;
  health: number; // 0..1
  status: "healthy" | "monitored" | "closed";
};
export type Train = {
  id: string;
  route: string[];
  passengers: number;
  // Live-twin fields (present when the backend is reachable)
  current_station?: string | null;
  route_index?: number;
  progress?: number; // 0..1 along the edge to the next station
  held?: boolean;
  delayed_minutes?: number;
};

const RAW_STATIONS: { id: string; name: string; lat: number; lon: number }[] = [
  { id: "NEW_DELHI", name: "New Delhi", lat: 28.6427, lon: 77.2207 },
  { id: "JAIPUR_JUNCT", name: "Jaipur", lat: 26.9196, lon: 75.7878 },
  { id: "MUMBAI_CENTR", name: "Mumbai", lat: 18.9696, lon: 72.8195 },
  { id: "HOWRAH_JUNCT", name: "Howrah", lat: 22.5839, lon: 88.3434 },
  { id: "CHENNAI_CHET", name: "Chennai", lat: 13.0827, lon: 80.2757 },
  { id: "BENGALURU_EA", name: "Bengaluru", lat: 12.9784, lon: 77.6408 },
  { id: "SECUNDERABAD", name: "Secunderabad", lat: 17.4344, lon: 78.5013 },
  { id: "AHMEDABAD_JU", name: "Ahmedabad", lat: 23.0263, lon: 72.6006 },
  { id: "BHOPAL_JUNCT", name: "Bhopal", lat: 23.268, lon: 77.4141 },
  { id: "LUCKNOW_JUNC", name: "Lucknow", lat: 26.8318, lon: 80.9214 },
  { id: "PATNA_JUNCTI", name: "Patna", lat: 25.6023, lon: 85.1356 },
  { id: "BHUBANESWAR", name: "Bhubaneswar", lat: 20.2661, lon: 85.8438 },
  { id: "GUWAHATI", name: "Guwahati", lat: 26.1811, lon: 91.7449 },
  { id: "NAGPUR_JUNCT", name: "Nagpur", lat: 21.1516, lon: 79.0882 },
  { id: "PUNE_JUNCTIO", name: "Pune", lat: 18.5289, lon: 73.8744 },
  { id: "THIRUVANANTH", name: "Trivandrum", lat: 8.4873, lon: 76.9525 },
  { id: "RANCHI", name: "Ranchi", lat: 23.3648, lon: 85.3358 },
  { id: "RAIPUR_JUNCT", name: "Raipur", lat: 21.2497, lon: 81.6396 },
  { id: "DEHRADUN", name: "Dehradun", lat: 30.3255, lon: 78.0329 },
  { id: "CHANDIGARH_R", name: "Chandigarh", lat: 30.7046, lon: 76.8179 },
  { id: "JAMMU_TAWI", name: "Jammu Tawi", lat: 32.7185, lon: 74.8664 },
];

// Project lat/lon into the 820×740 SVG viewBox.
const VIEW_W = 820;
const VIEW_H = 740;
const PAD_X = 80;
const PAD_TOP = 40;
const PAD_BOTTOM = 60;

const lats = RAW_STATIONS.map((s) => s.lat);
const lons = RAW_STATIONS.map((s) => s.lon);
const latMin = Math.min(...lats),
  latMax = Math.max(...lats);
const lonMin = Math.min(...lons),
  lonMax = Math.max(...lons);

export const STATIONS: Station[] = RAW_STATIONS.map((s) => ({
  ...s,
  x: Math.round(PAD_X + ((s.lon - lonMin) / (lonMax - lonMin)) * (VIEW_W - 2 * PAD_X)),
  y: Math.round(PAD_TOP + ((latMax - s.lat) / (latMax - latMin)) * (VIEW_H - PAD_TOP - PAD_BOTTOM)),
}));

const STATION_BY_ID = Object.fromEntries(STATIONS.map((s) => [s.id, s]));

function haversineKm(a: { lat: number; lon: number }, b: { lat: number; lon: number }) {
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lon - a.lon) * Math.PI) / 180;
  const p1 = (a.lat * Math.PI) / 180;
  const p2 = (b.lat * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dLon / 2) ** 2;
  return Math.round(R * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h)));
}

const RAW_TRACKS: { id: string; from: string; to: string; health: number }[] = [
  { id: "T01", from: "NEW_DELHI", to: "JAIPUR_JUNCT", health: 0.94 },
  { id: "T02", from: "NEW_DELHI", to: "LUCKNOW_JUNC", health: 0.91 },
  { id: "T03", from: "NEW_DELHI", to: "DEHRADUN", health: 0.88 },
  { id: "T04", from: "NEW_DELHI", to: "CHANDIGARH_R", health: 0.93 },
  { id: "T05", from: "CHANDIGARH_R", to: "JAMMU_TAWI", health: 0.82 },
  { id: "T06", from: "CHANDIGARH_R", to: "JAIPUR_JUNCT", health: 0.86 },
  { id: "T07", from: "JAMMU_TAWI", to: "DEHRADUN", health: 0.78 },
  { id: "T08", from: "DEHRADUN", to: "LUCKNOW_JUNC", health: 0.84 },
  { id: "T09", from: "LUCKNOW_JUNC", to: "PATNA_JUNCTI", health: 0.9 },
  { id: "T10", from: "PATNA_JUNCTI", to: "GUWAHATI", health: 0.83 },
  { id: "T11", from: "PATNA_JUNCTI", to: "RANCHI", health: 0.76 },
  { id: "T12", from: "PATNA_JUNCTI", to: "HOWRAH_JUNCT", health: 0.89 },
  { id: "T13", from: "JAIPUR_JUNCT", to: "BHOPAL_JUNCT", health: 0.87 },
  { id: "T14", from: "AHMEDABAD_JU", to: "BHOPAL_JUNCT", health: 0.92 },
  { id: "T15", from: "MUMBAI_CENTR", to: "AHMEDABAD_JU", health: 0.95 },
  { id: "T16", from: "AHMEDABAD_JU", to: "PUNE_JUNCTIO", health: 0.85 },
  { id: "T17", from: "MUMBAI_CENTR", to: "PUNE_JUNCTIO", health: 0.96 },
  { id: "T18", from: "MUMBAI_CENTR", to: "SECUNDERABAD", health: 0.81 },
  { id: "T19", from: "PUNE_JUNCTIO", to: "SECUNDERABAD", health: 0.88 },
  { id: "T20", from: "BHOPAL_JUNCT", to: "NAGPUR_JUNCT", health: 0.9 },
  { id: "T21", from: "BHOPAL_JUNCT", to: "RAIPUR_JUNCT", health: 0.68 },
  { id: "T22", from: "NAGPUR_JUNCT", to: "SECUNDERABAD", health: 0.93 },
  { id: "T23", from: "NAGPUR_JUNCT", to: "RAIPUR_JUNCT", health: 0.52 },
  { id: "T24", from: "RAIPUR_JUNCT", to: "RANCHI", health: 0.66 },
  { id: "T25", from: "RANCHI", to: "HOWRAH_JUNCT", health: 0.91 },
  { id: "T26", from: "RANCHI", to: "BHUBANESWAR", health: 0.79 },
  { id: "T27", from: "HOWRAH_JUNCT", to: "BHUBANESWAR", health: 0.94 },
  { id: "T28", from: "HOWRAH_JUNCT", to: "GUWAHATI", health: 0.75 },
  { id: "T29", from: "SECUNDERABAD", to: "CHENNAI_CHET", health: 0.92 },
  { id: "T30", from: "SECUNDERABAD", to: "BENGALURU_EA", health: 0.89 },
  { id: "T31", from: "CHENNAI_CHET", to: "BENGALURU_EA", health: 0.95 },
  { id: "T32", from: "CHENNAI_CHET", to: "THIRUVANANTH", health: 0.87 },
  { id: "T33", from: "BENGALURU_EA", to: "THIRUVANANTH", health: 0.86 },
];

export const TRACKS: Track[] = RAW_TRACKS.map((t) => ({
  ...t,
  length_km: haversineKm(STATION_BY_ID[t.from], STATION_BY_ID[t.to]),
  status: t.health <= 0.7 ? "monitored" : "healthy",
}));

// Fallback fleet used until a live run returns the twin's actual trains.
export const TRAINS: Train[] = [
  {
    id: "TR00",
    route: ["NEW_DELHI", "JAIPUR_JUNCT", "BHOPAL_JUNCT", "NAGPUR_JUNCT", "RAIPUR_JUNCT"],
    passengers: 312,
  },
  {
    id: "TR01",
    route: ["MUMBAI_CENTR", "PUNE_JUNCTIO", "SECUNDERABAD", "CHENNAI_CHET"],
    passengers: 410,
  },
  {
    id: "TR02",
    route: ["HOWRAH_JUNCT", "RANCHI", "RAIPUR_JUNCT", "NAGPUR_JUNCT"],
    passengers: 520,
  },
  { id: "TR03", route: ["NEW_DELHI", "LUCKNOW_JUNC", "PATNA_JUNCTI", "GUWAHATI"], passengers: 280 },
  {
    id: "TR04",
    route: ["SECUNDERABAD", "NAGPUR_JUNCT", "RAIPUR_JUNCT", "RANCHI", "HOWRAH_JUNCT"],
    passengers: 273,
  },
  { id: "TR05", route: ["CHENNAI_CHET", "BENGALURU_EA", "THIRUVANANTH"], passengers: 198 },
];

export function stationName(id: string): string {
  return STATION_BY_ID[id]?.name ?? id;
}

export function trackById(id: string): Track | undefined {
  return TRACKS.find((t) => t.id === id);
}

/** Shortest path (fewest hops) between two stations, skipping blocked tracks. */
export function findPath(
  from: string,
  to: string,
  blockedTrackIds: string[] = [],
): string[] | null {
  const blocked = new Set(blockedTrackIds);
  const adj = new Map<string, string[]>();
  for (const t of TRACKS) {
    if (blocked.has(t.id)) continue;
    if (!adj.has(t.from)) adj.set(t.from, []);
    if (!adj.has(t.to)) adj.set(t.to, []);
    adj.get(t.from)!.push(t.to);
    adj.get(t.to)!.push(t.from);
  }
  const prev = new Map<string, string>();
  const seen = new Set([from]);
  const queue = [from];
  while (queue.length) {
    const cur = queue.shift()!;
    if (cur === to) {
      const path = [to];
      while (path[0] !== from) path.unshift(prev.get(path[0])!);
      return path;
    }
    for (const next of adj.get(cur) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        prev.set(next, cur);
        queue.push(next);
      }
    }
  }
  return null;
}
