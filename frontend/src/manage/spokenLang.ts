// The language a new radio station is likely to speak, from the person's own radio stations: the
// one most of those on the same band speak, else most of those on the same receiver, else the
// stations file's default (`defaults.language`). Empty when none says, and the page asks: the
// browser's language, or the country's, says nothing about what is spoken on a channel. Plain
// functions, so they can be tested without a page.

/** One of the person's radio stations: where it is heard and the language set for it. */
export interface OnAir { device: string; mhz: number; language: string; watch?: unknown }

/** The radio stations as the radio view lists them on each receiver (`receivers`), with the
 * language and watch each has in the page's list of stations (`stations`). */
export function stationsOnAir(
  receivers: { id: string; stations: { id: string; mhz: number }[] }[],
  stations: { id: string; language: string; watch?: unknown }[],
): OnAir[] {
  const byId = new Map(stations.map((s) => [s.id, s]));
  return receivers.flatMap((r) => r.stations.flatMap((x) => {
    const s = byId.get(x.id);
    return s ? [{ device: r.id, mhz: x.mhz, language: s.language, watch: s.watch }] : [];
  }));
}

/** The language for a station at `mhz` on receiver `device`. `bandOf`: the id of the band a
 * frequency is in, null outside every band. A watch, or a station whose language is detected,
 * says nothing about the language. `byDefault`: the stations file's default language, if any. */
export function spokenLang(kept: OnAir[], at: { mhz: number; device: string }, bandOf: (mhz: number) => string | null,
                           byDefault?: string | null): string {
  const said = kept.filter((s) => s.watch == null && s.language && s.language !== "auto");
  const band = bandOf(at.mhz);
  return most(band ? said.filter((s) => bandOf(s.mhz) === band) : []) || most(said.filter((s) => s.device === at.device)) || byDefault || "";
}

/** The language most of them speak, the first of those on a tie; "" for none. */
function most(list: OnAir[]): string {
  const n = new Map<string, number>();
  for (const s of list) n.set(s.language, (n.get(s.language) || 0) + 1);
  let best = "";
  for (const [lang, count] of n) if (count > (n.get(best) || 0)) best = lang;
  return best;
}

/** What a request says of the language spoken: one language, or "auto" for the speech model to
 * tell each line's, with the languages it mostly is when the person gave any. `languages` goes
 * with "auto" only: the server refuses it beside one language. */
export function spokenFields(lang: string, languages: string[] = []): { language: string; languages?: string[] } {
  const language = lang.trim();
  return language === "auto" && languages.length ? { language, languages } : { language };
}
