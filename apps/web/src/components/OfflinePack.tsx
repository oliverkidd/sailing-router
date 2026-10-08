"use client";

import { useEffect, useState } from "react";
import {
  cacheStats,
  formatMegabytes,
  installDeviceCache,
  type CacheStats,
} from "@/lib/deviceCache";
import { downloadVoyageChart } from "@/lib/voyagePack";
import type { Course } from "@/lib/types";

installDeviceCache();

export default function OfflinePack({
  course,
  canDownload,
}: {
  course: Course;
  canDownload: boolean;
}) {
  const [stats, setStats] = useState<CacheStats | null>(null);
  const [online, setOnline] = useState(true);
  const [progress, setProgress] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* The page still works online if the worker is blocked. */
      });
    }
    const refresh = () => {
      cacheStats()
        .then(setStats)
        .catch(() => setStats(null));
    };
    refresh();
    const id = window.setInterval(refresh, 8000);
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    setOnline(navigator.onLine);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  const keep = async () => {
    if (!canDownload) return;
    setNote(null);
    try {
      await downloadVoyageChart(window.location.origin, course, (p) => {
        setProgress(`${p.done}/${p.total}`);
      });
      setStats(await cacheStats());
      setNote("Voyage chart kept on this device.");
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Could not save the chart.");
    } finally {
      setProgress(null);
    }
  };

  return (
    <div className="offline-pack">
      <div className="offline-pack-head">
        <strong>{online ? "Online" : "Offline"}</strong>
        <span>
          {stats
            ? `${formatMegabytes(stats.usedBytes)} / ${formatMegabytes(stats.budgetBytes)}`
            : "Cache"}
        </span>
      </div>
      <p className="offline-pack-note">
        Chart tiles stay on this device until the limit, then the oldest unused
        ones are dropped. A saved voyage is not dropped.
      </p>
      <button type="button" className="live-race-btn" onClick={keep} disabled={!canDownload || progress != null}>
        {progress ? `Saving chart ${progress}` : "Keep this course offline"}
      </button>
      {note && <p className="offline-pack-note">{note}</p>}
    </div>
  );
}
