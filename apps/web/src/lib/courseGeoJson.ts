// Convert between our Course model and a GeoJSON FeatureCollection for import/export.
import type { Course, LatLon, Mark } from "./types";

export function courseToGeoJson(course: Course): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [
    {
      type: "Feature",
      properties: { kind: "start", depart_utc: course.depart_utc, min_depth_m: course.min_depth_m },
      geometry: { type: "Point", coordinates: [course.start[1], course.start[0]] },
    },
    ...course.marks.map((m, i) => ({
      type: "Feature" as const,
      properties: { kind: "mark", order: i, radius_nm: m.radius_nm, rounding: m.rounding, name: m.name ?? null },
      geometry: { type: "Point" as const, coordinates: [m.position[1], m.position[0]] },
    })),
    {
      type: "Feature",
      properties: { kind: "finish" },
      geometry: { type: "Point", coordinates: [course.finish[1], course.finish[0]] },
    },
    ...course.exclusions.map((ring) => ({
      type: "Feature" as const,
      properties: { kind: "exclusion" },
      geometry: {
        type: "Polygon" as const,
        coordinates: [ring.map(([lat, lon]) => [lon, lat])],
      },
    })),
  ];
  return { type: "FeatureCollection", features };
}

export function courseFromGeoJson(gj: GeoJSON.FeatureCollection, base: Course): Course {
  let start: LatLon = base.start;
  let finish: LatLon = base.finish;
  const marks: Mark[] = [];
  const exclusions: LatLon[][] = [];
  let departUtc = base.depart_utc;
  let minDepth = base.min_depth_m;

  for (const f of gj.features) {
    const kind = (f.properties as Record<string, unknown> | null)?.kind;
    const g = f.geometry;
    if (g.type === "Point") {
      const [lon, lat] = g.coordinates as [number, number];
      if (kind === "start") {
        start = [lat, lon];
        const p = f.properties as Record<string, unknown>;
        if (typeof p.depart_utc === "string") departUtc = p.depart_utc;
        if (typeof p.min_depth_m === "number") minDepth = p.min_depth_m;
      } else if (kind === "finish") {
        finish = [lat, lon];
      } else {
        const p = (f.properties ?? {}) as Record<string, unknown>;
        marks.push({
          position: [lat, lon],
          radius_nm: typeof p.radius_nm === "number" ? p.radius_nm : 0.3,
          rounding: (p.rounding as Mark["rounding"]) ?? "either",
          name: (p.name as string | null) ?? null,
        });
      }
    } else if (g.type === "Polygon") {
      exclusions.push((g.coordinates[0] as [number, number][]).map(([lon, lat]) => [lat, lon]));
    }
  }
  return { start, finish, marks, exclusions, depart_utc: departUtc, min_depth_m: minDepth };
}
