// The language spoken on a station: one the person names, or Detect it, where the speech model
// tells each line's language (language "auto"), among the few the person expects if given.
import { useEffect, useRef } from "react";
import { Check, Field, LangChips, LangPicker } from "../components/ui";

const NAMED = "Its name in English or in the language itself, or a tag.";
const WHY = "Speech is written down in this language, so choose the one spoken there: the wrong one gives gibberish.";
const DETECT = "Needs a model that tells languages apart (Whisper) and is slower than a known language.";

/** `lang`: a language tag, "auto" to detect it, or "" until one is chosen. `langs`, `onLangs`:
 * the languages it mostly is, with Detect it; left out where they can't be given. `named`: what
 * the hint says while a language is named, such as where it came from. `focusIfEmpty`: the field
 * takes the focus when it opens with no language. */
export function SpokenField({ id, lang, onLang, langs, onLangs, known, named: hint, focusIfEmpty }: {
  id: string;
  lang: string;
  onLang: (tag: string) => void;
  langs?: string[];
  onLangs?: (tags: string[]) => void;
  known: string[];
  named?: string;
  focusIfEmpty?: boolean;
}) {
  const detect = lang === "auto";
  // The language named before Detect it was ticked, back when it is unticked.
  const named = useRef(detect ? "" : lang);
  if (!detect) named.current = lang;
  // After the frame in which the form that holds it is brought into view and focused.
  useEffect(() => {
    if (focusIfEmpty && !lang) requestAnimationFrame(() => document.getElementById(id)?.focus({ preventScroll: true }));
  }, []);
  return (
    <>
      <Field id={detect ? undefined : id} label="Language spoken" hint={detect ? DETECT : lang ? hint || NAMED : WHY}>
        <div className="m-spoken">
          {!detect && <LangPicker id={id} value={lang} onChange={onLang} options={known} />}
          <Check checked={detect} onChange={(on) => onLang(on ? "auto" : named.current)}>Detect it</Check>
        </div>
      </Field>
      {detect && onLangs && (
        <Field label="Mostly these languages" hint="Optional: leave it empty for any language the model tells apart.">
          <LangChips value={langs || []} onChange={onLangs} known={known} label="Mostly these languages" />
        </Field>
      )}
    </>
  );
}
