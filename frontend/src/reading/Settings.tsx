// Display settings: this reader's language, text size and theme. Kept in this browser. Which
// stations this device shows is chosen in the station list itself (Edit list).
import { useRef, useState } from "react";
import { langName, langNameEn, sameLang, shownLang } from "../lib/lang";
import { S } from "../lib/live";
import { P, type Size, type Theme, setPrefs, visible } from "../lib/prefs";
import { useTopics } from "../lib/store";
import { Button, Check, Segmented, Select, useDismiss } from "../components/ui";
import { T } from "./strings";

export function DisplayButton() {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  useDismiss(open, () => setOpen(false), [btn, pop]);
  return (
    <span className="menu-anchor">
      <Button ref={btn} variant="ghost" icon="type" aria-label={T.display} title={T.display}
              aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)} />
      {open && (
        <div ref={pop} className="pop settings" role="dialog" aria-label={T.display}>
          <DisplaySettings />
        </div>
      )}
    </span>
  );
}

const SIZES: { value: Size; label: React.ReactNode; name: string }[] = [
  { value: "s", label: <span style={{ fontSize: 12 }}>A</span>, name: "Small" },
  { value: "m", label: <span style={{ fontSize: 14 }}>A</span>, name: "Medium" },
  { value: "l", label: <span style={{ fontSize: 17 }}>A</span>, name: "Large" },
  { value: "xl", label: <span style={{ fontSize: 20 }}>A</span>, name: "Extra large" },
];

function DisplaySettings() {
  useTopics("prefs", "state");
  const targets = [...new Set(S.stations.flatMap((s) => s.translate_to))];
  const missing = P.lang ? S.stations.filter((s) => visible(s.id) && !sameLang(shownLang(s, P.lang), P.lang)) : [];
  const models = [...new Set(S.stations.map((s) => s.asr_model).filter(Boolean))];
  return (
    <div className="settings-body">
      <div className="set-row">
        <label className="set-label" htmlFor="pref-lang">{T.readIn}</label>
        <Select id="pref-lang" value={targets.includes(P.lang || "") ? P.lang! : ""} onChange={(e) => setPrefs({ lang: e.target.value })}
                options={[["", T.originalLanguage], ...targets.map((t) => [t, langName(t)] as [string, string])]} />
        {missing.length > 0 && (
          <p className="set-hint">
            Not translated into {langNameEn(P.lang!)}: {missing.map((s) => s.name).join(", ")}. They show their own language or their first translation.
          </p>
        )}
      </div>
      <div className="set-row">
        <span className="set-label" id="pref-size">{T.textSize}</span>
        <Segmented label={T.textSize} value={P.size} onChange={(size) => setPrefs({ size })} options={SIZES} />
      </div>
      <Check checked={P.src} onChange={(src) => setPrefs({ src })}>{T.showOriginal}</Check>
      <div className="set-row">
        <span className="set-label">{T.theme}</span>
        <Segmented<Theme> label={T.theme} value={P.theme} onChange={(theme) => setPrefs({ theme })}
                          options={[{ value: "", label: "Auto" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }]} />
      </div>
      <p className="set-about">
        Offline on this machine. Speech: {models.join(", ") || "–"}{S.mtModel ? ` · translation: ${S.mtModel}` : ""}.
        {S.noncommercial.length > 0 && <> Non-commercial use only: {S.noncommercial.join(", ")}.</>}
      </p>
    </div>
  );
}
