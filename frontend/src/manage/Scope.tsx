// The scope: a receiver's spectrum and its waterfall on the instrument's dark band, with the
// tuning drawn in the pointer's orange. It draws what it is given and reports clicks, steps and
// the wheel; tuning, polling and zoom stay with whoever holds the receiver (Listen's tuner).
import { useEffect, useMemo, useRef, useState } from "react";
import { type Band, fmtMhz } from "./bands";

/** One picture of what a receiver hears: the power of each bin from start_hz to stop_hz. */
export interface Spectrum {
  start_hz: number;
  stop_hz: number;
  db: number[];
  noise_db: number;
  /** The share of samples at the converter's limit; above 0.001 the trace turns to trouble. */
  clipped?: number;
}

/** A name printed at the top of the scope above its frequency, such as a station on the receiver. */
export interface Marker {
  mhz: number;
  name: string;
}

export interface ScopeProps {
  /** The latest spectrum; null while the receiver has sent none, which leaves the scope blank. */
  spectrum: Spectrum | null;
  /** What the receiver hears, in MHz: the scale until the first spectrum arrives. */
  centerMhz: number;
  spanMhz: number;
  /** A zoomed part of it, in MHz, or null for all of it. */
  view: [number, number] | null;
  /** The tuning: where it is, its demodulation (how frequencies are written) and channel width. */
  tunedMhz: number;
  mode: string;
  channelKhz: number;
  /** A click lands on the band's nearest channel, else on this raster. */
  stepKhz: number;
  band: Band | null;
  markers: Marker[];
  onTune: (mhz: number) => void;
  /** Steps of the raster: the wheel and the arrow keys (ten with Shift). */
  onStep: (n: number) => void;
}

/** The nearest channel: the band's channel list, else its raster from the band's start. */
export function snap(mhz: number, stepKhz: number, band: Band | null): number {
  if (band?.channels_mhz.length && band.start_mhz <= mhz && mhz <= band.stop_mhz)
    return band.channels_mhz.reduce((p, c) => (Math.abs(c - mhz) < Math.abs(p - mhz) ? c : p));
  const step = stepKhz / 1000;
  const origin = band && band.step_khz && Math.abs(band.step_khz - stepKhz) < 1e-6 ? band.start_mhz : 0;
  return +(origin + Math.round((mhz - origin) / step) * step).toFixed(5);
}

const niceStep = (span: number) => [0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1].find((s) => span / s <= 8) || 1;

/** The spectrum and its waterfall, on the instrument's dark band. The orange line and shading
 * are the tuning and its channel; a click tunes, the wheel and arrow keys step. */
export function Scope({ spectrum: s, centerMhz, spanMhz, view, tunedMhz, mode, channelKhz, stepKhz, band, markers, onTune, onStep }: ScopeProps) {
  const spec = useRef<HTMLCanvasElement>(null);
  const fall = useRef<HTMLCanvasElement>(null);
  const span = useRef<string>("");
  const [hover, setHover] = useState<number | null>(null);
  const range = useMemo(() => (s ? [s.start_hz / 1e6, s.stop_hz / 1e6] : view || [centerMhz - spanMhz / 2, centerMhz + spanMhz / 2]), [s, view, centerMhz, spanMhz]);
  const at = (clientX: number) => {
    const r = spec.current!.getBoundingClientRect();
    return range[0] + ((clientX - r.left) / r.width) * (range[1] - range[0]);
  };
  useEffect(() => {
    const c = spec.current;
    if (!c) return;
    let acc = 0; // a trackpad sends many small deltas: one step per notch's worth
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      acc += e.deltaY;
      if (Math.abs(acc) < 40) return;
      onStep(acc > 0 ? -1 : 1);
      acc = 0;
    };
    c.addEventListener("wheel", wheel, { passive: false });
    return () => c.removeEventListener("wheel", wheel);
  }, [onStep]);
  useEffect(() => {
    const c = spec.current;
    if (!c) return;
    // Nothing recent from the receiver: a blank scope, not the last picture standing still.
    if (!s) { c.getContext("2d")!.clearRect(0, 0, c.width, c.height); return; }
    const css = getComputedStyle(document.documentElement);
    const tok = (n: string) => css.getPropertyValue(n).trim() || "#888";
    drawSpectrum(c, s, tunedMhz, channelKhz, markers, hover, tok);
  }, [s, hover]);
  useEffect(() => {
    // the waterfall scrolls once per new spectrum, never on pointer moves
    if (!s || !fall.current) return;
    const key = `${s.start_hz}-${s.stop_hz}`;
    if (key !== span.current) { span.current = key; fall.current.getContext("2d")!.clearRect(0, 0, fall.current.width, fall.current.height); }
    drawFall(fall.current, s);
  }, [s]);
  return (
    <figure className="lv-scope">
      <canvas ref={spec} className="lv-spectrum" width={1200} height={220} tabIndex={0} role="img"
              aria-label={`Spectrum from ${range[0].toFixed(3)} to ${range[1].toFixed(3)} MHz, tuned to ${fmtMhz(tunedMhz, mode)} MHz. Arrow keys step the tuning.`}
              onPointerMove={(e) => setHover(at(e.clientX))} onPointerLeave={() => setHover(null)}
              onClick={(e) => onTune(snap(at(e.clientX), stepKhz, band))}
              onKeyDown={(e) => {
                if (e.key === "ArrowRight" || e.key === "ArrowUp") { e.preventDefault(); onStep(e.shiftKey ? 10 : 1); }
                if (e.key === "ArrowLeft" || e.key === "ArrowDown") { e.preventDefault(); onStep(e.shiftKey ? -10 : -1); }
              }} />
      <canvas ref={fall} className="lv-fall" width={1200} height={200} role="img" aria-label="Waterfall: the spectrum over the last minute" />
      <figcaption className="m-hint lv-readout">
        {hover != null ? <>Pointer at <span className="mono">{fmtMhz(snap(hover, stepKhz, band), mode)}</span> MHz · click to tune</> : s ? <>Noise floor {s.noise_db.toFixed(0)} dB</> : "Waiting for the receiver…"}
      </figcaption>
    </figure>
  );
}

function drawSpectrum(canvas: HTMLCanvasElement, s: Spectrum, tunedMhz: number, channelKhz: number, markers: Marker[], hover: number | null, tok: (n: string) => string) {
  const ctx = canvas.getContext("2d")!;
  const W = canvas.width, H = canvas.height, AX = 24; // the frequency scale along the bottom
  const f0 = s.start_hz / 1e6, f1 = s.stop_hz / 1e6;
  const x = (mhz: number) => ((mhz - f0) / (f1 - f0)) * W;
  const top = Math.ceil((Math.max(...s.db) + 5) / 10) * 10;
  const lo = Math.floor((s.noise_db - 8) / 10) * 10, hi = Math.max(lo + 40, Math.min(0, top));
  const y = (db: number) => (H - AX) - ((Math.max(lo, Math.min(hi, db)) - lo) / (hi - lo)) * (H - AX);
  ctx.clearRect(0, 0, W, H);
  ctx.font = "18px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.strokeStyle = tok("--band-tick");
  ctx.fillStyle = tok("--band-ink-2");
  for (let db = lo + 10; db < hi; db += 10) {
    ctx.beginPath(); ctx.moveTo(0, y(db)); ctx.lineTo(W, y(db)); ctx.stroke();
    ctx.fillText(`${db}`, 6, y(db) - 4);
  }
  const tick = niceStep(f1 - f0);
  for (let f = Math.ceil(f0 / tick) * tick; f <= f1; f += tick) {
    const px = x(f);
    ctx.beginPath(); ctx.moveTo(px, H - AX); ctx.lineTo(px, H - AX + 6); ctx.stroke();
    const label = f.toFixed(tick < 0.01 ? 4 : tick < 0.1 ? 3 : tick < 1 ? 2 : 1);
    ctx.fillText(label, Math.min(W - ctx.measureText(label).width - 4, Math.max(2, px - ctx.measureText(label).width / 2)), H - 4);
  }
  for (const m of markers) {
    if (m.mhz < f0 || m.mhz > f1) continue;
    ctx.fillStyle = tok("--band-ink-2");
    ctx.fillRect(x(m.mhz) - 0.5, 0, 1, 10);
    ctx.fillText(m.name, Math.min(W - 160, x(m.mhz) + 4), 16);
  }
  // the tuning: its channel shaded, its centre a line, in the pointer's orange
  const half = channelKhz / 2000;
  ctx.fillStyle = tok("--accent-tint");
  ctx.fillRect(x(tunedMhz - half), 0, Math.max(2, x(tunedMhz + half) - x(tunedMhz - half)), H - AX);
  ctx.fillStyle = tok("--accent");
  ctx.fillRect(x(tunedMhz) - 1, 0, 2, H - AX);
  if (hover != null) {
    ctx.fillStyle = tok("--band-ink-2");
    ctx.fillRect(x(hover) - 0.5, 0, 1, H - AX);
  }
  ctx.strokeStyle = (s.clipped ?? 0) > 0.001 ? tok("--band-lamp-trouble") : tok("--band-ink");
  ctx.lineWidth = 2;
  ctx.beginPath();
  s.db.forEach((v: number, i: number) => {
    const px = ((i + 0.5) / s.db.length) * W;
    if (i) ctx.lineTo(px, y(v));
    else ctx.moveTo(px, y(v));
  });
  ctx.stroke();
}

/** One row per poll, scrolling down, scaled from the noise floor up: noise stays near the band's
 * black and signal warms through the accent's hue to near white (strength reads as brightness). */
function drawFall(canvas: HTMLCanvasElement, s: Spectrum) {
  const ctx = canvas.getContext("2d")!;
  const W = canvas.width, H = canvas.height;
  ctx.drawImage(canvas, 0, 0, W, H - 1, 0, 1, W, H - 1);
  const row = ctx.createImageData(W, 1);
  const n = s.db.length;
  const stops: [number, number, number][] = [[12, 12, 11], [74, 40, 22], [210, 78, 20], [242, 150, 80], [255, 240, 225]];
  for (let px = 0; px < W; px++) {
    const v = s.db[Math.min(n - 1, Math.floor((px / W) * n))] - s.noise_db;
    const t = Math.max(0, Math.min(1, (v + 3) / 40)) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(t)), f = t - i;
    const c = stops[i].map((a, k) => a + (stops[i + 1][k] - a) * f);
    row.data.set([c[0], c[1], c[2], 255], px * 4);
  }
  ctx.putImageData(row, 0, 0);
}
