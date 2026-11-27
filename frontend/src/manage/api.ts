// Radio's and Settings' calls. /api/manage answers only the Wavescribe machine itself, and every
// change carries the X-Wavescribe header.
import { useCallback, useEffect, useRef, useState } from "react";
import { LANGS, countryLang, langNameEn } from "../lib/lang";
import { S, stationsChanged } from "../lib/live";
import { P } from "../lib/prefs";
import { emit } from "../lib/store";

export type Any = any;

export async function api<T = Any>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { "X-Wavescribe": "1" } };
  if (body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(`/api/manage${path}`, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(data.error || `${r.status} ${r.statusText}`, data);
  return data as T;
}

/** A refusal with what the page needs to ask next (`code`: acknowledge, busy, retune…). */
export class ApiError extends Error {
  code?: string;
  data: Any;
  constructor(message: string, data: Any) {
    super(message);
    this.code = data?.code;
    this.data = data;
  }
}
export const get = <T = Any>(path: string) => api<T>("GET", path);
export const post = <T = Any>(path: string, body?: unknown) => api<T>("POST", path, body ?? {});
export const del = <T = Any>(path: string) => api<T>("DELETE", path);

/** Loads once, keeps the last good answer, and reloads on demand. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = useRef(true);
  const reload = useCallback(async () => {
    try {
      const d = await fn();
      if (live.current) { setData(d); setError(null); }
    } catch (e) {
      if (live.current) setError((e as Error).message);
    }
  }, deps);
  useEffect(() => {
    live.current = true;
    reload();
    return () => { live.current = false; };
  }, [reload]);
  return { data, error, reload, setData };
}

/** Runs `fn` every `ms` while the page is visible. */
export function useEvery(ms: number, fn: () => unknown, on = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!on) return;
    // A failed poll (the server restarting, say) is retried on the next tick, not left unhandled.
    const id = setInterval(() => { if (!document.hidden) Promise.resolve().then(ref.current).catch(() => {}); }, ms);
    return () => clearInterval(id);
  }, [ms, on]);
}

/** Opens Tune by hand on a frequency (from a scan result, say). */
export const HANDOFF = "ws-listen";
export function listenTo(mhz: number, mode?: string | null, device?: string) {
  try {
    sessionStorage.setItem(HANDOFF, JSON.stringify({ mhz, mode: mode || undefined, device }));
  } catch {
    /* without storage the page opens untuned */
  }
  location.hash = "#/listen";
}

/** After a station change, the reading view reloads its stations too, in every tab. */
export const refreshPage = () => stationsChanged().catch(() => {});

// -- formatting ------------------------------------------------------------------------------
export const num = (v: number | null | undefined, d = 1) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
/** 43 MB, 734 MB, 1.2 GB: megabytes in, the unit that keeps the number readable out. */
export const mb = (v: number | null | undefined) =>
  v == null ? "–" : v >= 1000 ? `${(v / 1024).toFixed(1)} GB` : v >= 10 ? `${Math.round(v)} MB` : `${v.toFixed(1)} MB`;
export const bytes = (b: number | null | undefined) => (b == null ? "–" : mb(b / 1048576));
export const gbSize = (g: number | null | undefined) => (g == null ? "size unknown" : mb(g * 1024));
export const secs = (s: number | null | undefined) => (s == null ? "–" : s < 10 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`);

// What the speech models on this machine can write down, asked for once (Settings re-renders on
// "langs" when it arrives).
const modelLangs = new Set<string>();
let askedModels = false;
export function loadModelLangs() {
  if (askedModels) return;
  askedModels = true;
  get("/models").then((v) => {
    for (const m of v.models || []) for (const l of m.languages || []) if (l !== "*") modelLangs.add(l);
    emit("langs");
  }).catch(() => { askedModels = false; });
}

/** Languages to offer, the likeliest first: this reader's, then the stations' own and their
 * translations, then every other one by its English name. */
export function knownLangs(stations: { language: string; translate_to?: string[] }[] = S.stations): string[] {
  const used = stations.flatMap((s) => [s.language, ...(s.translate_to || [])]).filter((t) => t && t !== "auto");
  const rest = [...new Set([...LANGS, ...modelLangs])].sort((a, b) => langNameEn(a).localeCompare(langNameEn(b)));
  return [...new Set([...(P.lang ? [P.lang] : []), ...used, ...rest])];
}

/** The reader's own language, offered as a translation unless the station already speaks it. */
export const readerLang = (spoken: string): string[] => (P.lang && primaryOf(P.lang) !== primaryOf(spoken) ? [P.lang] : []);
const primaryOf = (t: string) => (t || "").split("-")[0].toLowerCase();

/** The language a new station on the air most likely speaks: the default set for new stations,
 * else the language of the machine's country (where the server says it), else the one most of
 * this machine's radio stations speak. Empty when nothing says. */
export function localLang(radio: Any): string {
  const said = S.stations.filter((s) => s.kind === "sdr" && s.language && s.language !== "auto").map((s) => s.language);
  const most = [...new Set(said)].sort((a, b) => said.filter((x) => x === b).length - said.filter((x) => x === a).length)[0];
  return radio?.defaults?.language || countryLang(radio?.country) || most || "";
}

export const SEVERITIES = ["critical", "high", "medium", "low"] as const;
export const SEV_WORD: Record<string, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
