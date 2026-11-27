// Where a column's lines were heard, as the server names it (`freq_hz` and `label` on each
// utterance). Plain functions, so they can be tested without a page.

interface Heard {
  freq_hz?: number | null;
  label?: string | null;
}

/** "446.00625 MHz · PMR446 walkie-talkies, channel 1": the frequency, and the band plan's name
 * for it where the server has one. Null for a line with no frequency (a stream's). */
export function channelText(u: Heard): string | null {
  if (u.freq_hz == null) return null;
  const mhz = (u.freq_hz / 1e6).toFixed(5).replace(/(\.\d{3}\d*?)0+$/, "$1");
  return u.label ? `${mhz} MHz · ${u.label}` : `${mhz} MHz`;
}

/** For each paragraph, whether it says its channel: in a watch's column the first of each run of
 * paragraphs on one channel, so a conversation on one channel is named once; nowhere in another
 * station's column, whose channel never changes and is the station itself. A paragraph's
 * channel is its first line's (a watch's paragraphs keep to one channel). */
export function channelHeads(paras: { utts: Heard[] }[], watch: boolean): boolean[] {
  let last: number | null = null;
  return paras.map((p) => {
    const f = p.utts[0]?.freq_hz ?? null;
    const head = watch && f != null && f !== last;
    last = f;
    return head;
  });
}
