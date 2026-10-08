/**
 * On-device byte cache for chart ranges and terrain tiles.
 *
 * The nautical chart is two PMTiles archives (coastline ~575 MB, depth ~805 MB).
 * GitHub rejects a file over 100 MB, and putting a split copy of that chart in
 * the repo would make every clone enormous. The archives belong on a CDN.
 * This cache stores only the byte ranges and tiles this browser has actually
 * fetched, evicts the least recently used ones past a budget, and never evicts
 * a pinned voyage.
 */

const DB_NAME = "mw-device-cache";
const DB_VERSION = 1;
const BLOBS = "blobs";
const INDEX = "index";

const MOBILE_CAP = 400 * 1024 * 1024;
const DESKTOP_CAP = 1200 * 1024 * 1024;
const MIN_BUDGET = 80 * 1024 * 1024;

export interface CacheStats {
  usedBytes: number;
  budgetBytes: number;
  pinnedBytes: number;
  entries: number;
}

interface IndexRow {
  key: string;
  bytesLength: number;
  touched: number;
  pinned: number;
}

interface BlobRow {
  key: string;
  bytes: ArrayBuffer;
  status: number;
  headers: [string, string][];
}

let installed = false;
let dbPromise: Promise<IDBDatabase> | null = null;
let budgetPromise: Promise<number> | null = null;

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(BLOBS)) db.createObjectStore(BLOBS, { keyPath: "key" });
      if (!db.objectStoreNames.contains(INDEX)) db.createObjectStore(INDEX, { keyPath: "key" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

function isMobile(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
}

export async function cacheBudgetBytes(): Promise<number> {
  if (budgetPromise) return budgetPromise;
  budgetPromise = (async () => {
    const cap = isMobile() ? MOBILE_CAP : DESKTOP_CAP;
    let share = cap;
    try {
      const estimate = await navigator.storage?.estimate?.();
      if (estimate?.quota && estimate.quota > 0) {
        share = Math.min(cap, Math.floor(estimate.quota * 0.2));
      }
    } catch {
      /* private mode or an old browser: use the device cap */
    }
    return Math.max(MIN_BUDGET, share);
  })();
  return budgetPromise;
}

function cacheableUrl(url: string): boolean {
  try {
    const parsed = new URL(url, typeof location !== "undefined" ? location.href : undefined);
    return parsed.pathname.includes("/geo/");
  } catch {
    return false;
  }
}

function requestKey(url: string, init?: RequestInit): string {
  const headers = new Headers(init?.headers);
  const range = headers.get("Range") ?? "";
  return `${url}|${range}`;
}

async function readIndex(): Promise<IndexRow[]> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(INDEX, "readonly");
    const req = tx.objectStore(INDEX).getAll();
    req.onsuccess = () => resolve((req.result as IndexRow[]) ?? []);
    req.onerror = () => reject(req.error);
  });
}

export async function cacheStats(): Promise<CacheStats> {
  const [rows, budgetBytes] = await Promise.all([readIndex(), cacheBudgetBytes()]);
  let usedBytes = 0;
  let pinnedBytes = 0;
  for (const row of rows) {
    usedBytes += row.bytesLength;
    if (row.pinned) pinnedBytes += row.bytesLength;
  }
  return { usedBytes, budgetBytes, pinnedBytes, entries: rows.length };
}

async function touch(key: string): Promise<void> {
  const db = await openDb();
  const tx = db.transaction(INDEX, "readwrite");
  const store = tx.objectStore(INDEX);
  const existing = await new Promise<IndexRow | undefined>((resolve, reject) => {
    const req = store.get(key);
    req.onsuccess = () => resolve(req.result as IndexRow | undefined);
    req.onerror = () => reject(req.error);
  });
  if (existing) {
    existing.touched = Date.now();
    store.put(existing);
  }
  await txDone(tx);
}

async function readBlob(key: string): Promise<BlobRow | undefined> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(BLOBS, "readonly");
    const req = tx.objectStore(BLOBS).get(key);
    req.onsuccess = () => resolve(req.result as BlobRow | undefined);
    req.onerror = () => reject(req.error);
  });
}

function responseFrom(row: BlobRow): Response {
  return new Response(row.bytes, {
    status: row.status,
    statusText: row.status === 206 ? "Partial Content" : "OK",
    headers: row.headers,
  });
}

async function evictUntil(targetBytes: number): Promise<void> {
  const rows = (await readIndex())
    .filter((row) => !row.pinned)
    .sort((a, b) => a.touched - b.touched);
  let used = rows.reduce((sum, row) => sum + row.bytesLength, 0);
  const pinned = (await readIndex()).filter((row) => row.pinned);
  used += pinned.reduce((sum, row) => sum + row.bytesLength, 0);
  const db = await openDb();
  for (const row of rows) {
    if (used <= targetBytes) break;
    const tx = db.transaction([BLOBS, INDEX], "readwrite");
    tx.objectStore(BLOBS).delete(row.key);
    tx.objectStore(INDEX).delete(row.key);
    await txDone(tx);
    used -= row.bytesLength;
  }
}

async function putEntry(key: string, bytes: ArrayBuffer, status: number, headers: [string, string][]): Promise<void> {
  const budget = await cacheBudgetBytes();
  const rows = await readIndex();
  const previous = rows.find((row) => row.key === key);
  let used = rows.reduce((sum, row) => sum + row.bytesLength, 0);
  if (previous) used -= previous.bytesLength;
  const nextUsed = used + bytes.byteLength;
  if (nextUsed > budget) {
    await evictUntil(Math.floor(budget * 0.85) - bytes.byteLength);
  }
  const db = await openDb();
  const tx = db.transaction([BLOBS, INDEX], "readwrite");
  const pinned = previous?.pinned ?? 0;
  tx.objectStore(BLOBS).put({ key, bytes, status, headers } satisfies BlobRow);
  tx.objectStore(INDEX).put({
    key,
    bytesLength: bytes.byteLength,
    touched: Date.now(),
    pinned,
  } satisfies IndexRow);
  await txDone(tx);
}

/** Mark every cached chart byte as part of the voyage so eviction leaves it. */
export async function pinChartCache(): Promise<void> {
  const db = await openDb();
  const rows = await readIndex();
  const tx = db.transaction(INDEX, "readwrite");
  const store = tx.objectStore(INDEX);
  for (const row of rows) {
    if (row.key.includes("/geo/")) {
      row.pinned = 1;
      store.put(row);
    }
  }
  await txDone(tx);
}

async function cachedFetch(original: typeof fetch, input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  if (!cacheableUrl(url) || (init?.method && init.method !== "GET")) {
    return original(input, init);
  }
  const key = requestKey(url, init);
  try {
    const res = await original(input, init);
    if (!res.ok && res.status !== 206) return res;
    const bytes = await res.arrayBuffer();
    const headers = [...res.headers.entries()];
    await putEntry(key, bytes, res.status, headers);
    return responseFrom({ key, bytes, status: res.status, headers });
  } catch (err) {
    const hit = await readBlob(key);
    if (hit) {
      await touch(key);
      return responseFrom(hit);
    }
    throw err;
  }
}

/** Route chart and terrain fetches through the device cache. Call once, early. */
export function installDeviceCache(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => cachedFetch(original, input, init);
}

export function formatMegabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(0)} MB`;
}
