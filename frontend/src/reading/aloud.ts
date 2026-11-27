// Reading a summary aloud on the Wavescribe machine, as something any answer with points can
// have done: asked with the summary (`speak`), or later by its id (POST /api/digest/speak), and
// stopped (POST /api/digest/speech/stop). The machine reads one summary at a time, and an
// alert's voice cuts it short. Only this machine may: the speakers are in its room. The page
// can't hear the reading end, so how long it takes is an estimate (digest.ts, spokenSeconds).
import { fetchRead } from "../lib/auth";
import { emit } from "../lib/store";
import { type Answer, type SpeakResult, speakResult, spokenSeconds } from "./digest";
import { T } from "./strings";

/** By answer id: "starting" (asked), "reading" (began at `at`, about `seconds` long),
 * "stopping", "stopped" (by Stop, at `at`; `other` when Stop ended another summary this one
 * waited for), "ended" (Stop found nothing being read), "refused" (`code`, `text`), "gone" (no
 * longer kept there) or "failed" (`text`). */
export interface Aloud {
  state: "starting" | "reading" | "stopping" | "stopped" | "ended" | "refused" | "gone" | "failed";
  at: number;
  seconds: number;
  code: string | null;
  text: string;
  other?: boolean;
}

export const Aloud: Record<string, Aloud> = {};

const now = () => Date.now() / 1000;

function set(id: string, rec: Partial<Aloud> & Pick<Aloud, "state">) {
  Aloud[id] = { at: 0, seconds: 0, code: null, text: "", ...rec };
  emit("aloud");
}

/** Takes in what asking came to, for the answer `a`. */
export function heard(a: Answer, r: SpeakResult) {
  if (!a.id) return;
  if (r.kind === "started") set(a.id, { state: "reading", at: now(), seconds: spokenSeconds(a) });
  else set(a.id, { state: r.kind, code: r.code, text: r.text });
}

async function send(url: string, body?: unknown): Promise<{ status: number; data: unknown } | null> {
  try {
    const r = await fetchRead(url, {
      method: "POST",
      headers: { "X-Wavescribe": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    return { status: r.status, data: await r.json().catch(() => null) };
  } catch {
    return null;
  }
}

/** Asks the Wavescribe machine to read `a` aloud. */
export async function readAloud(a: Answer) {
  const id = a.id;
  if (!id || Aloud[id]?.state === "starting") return;
  set(id, { state: "starting" });
  const r = await send("/api/digest/speak", { id });
  if (!r) return set(id, { state: "failed", text: T.cantReach });
  heard(a, speakResult(r.status, r.data));
}

/** Stops the summary being read aloud, whichever it is: Stop under the answer `id` names, which
 * is being read or was refused while another one was. */
export async function stopReading(id: string) {
  const other = Aloud[id]?.state === "refused";
  set(id, { ...Aloud[id], state: "stopping" });
  const r = await send("/api/digest/speech/stop");
  if (!r || r.status !== 200) return set(id, { state: "failed", text: r ? speakResult(r.status, r.data).text : T.cantReach });
  const stopped = (r.data as { stopped?: unknown } | null)?.stopped === true;
  for (const [was, rec] of Object.entries(Aloud)) {
    if (was !== id && rec.state === "reading") Aloud[was] = { ...rec, state: "stopped", at: now() };
  }
  set(id, { state: stopped ? "stopped" : "ended", at: now(), other });
}
