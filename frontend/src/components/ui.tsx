// The page's controls. Standard web controls, drawn in Wavescribe's tokens (ui.css).
import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  forwardRef,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { emit, useTopics } from "../lib/store";
import { langName } from "../lib/lang";
import type { Lamp as LampState } from "../lib/station";
import { Icon, type IconName } from "./Icon";

const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");
export { cx };

type Variant = "primary" | "secondary" | "ghost" | "danger" | "band";
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  icon?: IconName;
  busy?: boolean;
}
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon, busy, className, children, type = "button", disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("btn", `btn-${variant}`, `btn-${size}`, busy && "is-busy", !children && "btn-icon", className)}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {icon && <Icon name={icon} size={size === "sm" ? 14 : 16} />}
      {children}
    </button>
  );
});

/** A link styled as a button. */
export function LinkButton({
  href, children, icon, variant = "secondary", size = "md", ...rest
}: { href: string; children: ReactNode; icon?: IconName; variant?: Variant; size?: "sm" | "md"; target?: string; download?: boolean }) {
  return (
    <a className={cx("btn", `btn-${variant}`, `btn-${size}`)} href={href} {...rest}
       rel={rest.target ? "noopener" : undefined}>
      {icon && <Icon name={icon} size={size === "sm" ? 14 : 16} />}
      {children}
    </a>
  );
}

/** An indicator lamp. Shape carries the state as well as colour: a filled lamp is on air, a
 * ring is on air with nothing to transcribe, an empty socket is off. */
export function Lamp({ state, onBand = false, label }: { state: LampState; onBand?: boolean; label?: string }) {
  return (
    <span
      className={cx("lamp", `lamp-${state}`, onBand && "on-band")}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}

export const Kbd = ({ children }: { children: ReactNode }) => <kbd className="kbd">{children}</kbd>;

export interface SegOption<T extends string> {
  value: T;
  label: ReactNode;
  name?: string; // accessible name when the label is visual
}
export function Segmented<T extends string>({
  options, value, onChange, label,
}: { options: SegOption<T>[]; value: T; onChange: (v: T) => void; label: string }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} aria-label={o.name}
                onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Check({ checked, onChange, children, disabled }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode; disabled?: boolean }) {
  return (
    <label className={cx("check", disabled && "is-disabled")}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="check-box" aria-hidden="true"><Icon name="check" size={12} /></span>
      <span>{children}</span>
    </label>
  );
}

/** An on/off switch with a visible label or an accessible one. */
export function Switch({ checked, onChange, label, hideLabel }: { checked: boolean; onChange: (v: boolean) => void; label: string; hideLabel?: boolean }) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)}
             aria-label={hideLabel ? label : undefined} />
      <span className="switch-track" aria-hidden="true"><span className="switch-knob" /></span>
      {!hideLabel && <span>{label}</span>}
    </label>
  );
}

export function Field({ label, hint, children, wide, id }: { label: ReactNode; hint?: ReactNode; children: ReactNode; wide?: boolean; id?: string }) {
  const auto = useId();
  const hintId = hint ? `${id || auto}-hint` : undefined;
  return (
    <div className={cx("field", wide && "field-wide")}>
      <label className="field-label" htmlFor={id}>{label}</label>
      {children}
      {hint && <p className="field-hint" id={hintId}>{hint}</p>}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={cx("input", className)} {...rest} />;
});
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...rest }, ref) {
  return <textarea ref={ref} className={cx("input", "textarea", className)} {...rest} />;
});
export function Select({ options, className, ...rest }: SelectHTMLAttributes<HTMLSelectElement> & { options: (string | [string, string])[] }) {
  return (
    <span className={cx("select", className)}>
      <select {...rest}>
        {options.map((o) => {
          const [v, l] = Array.isArray(o) ? o : [o, o];
          return <option key={v} value={v}>{l}</option>;
        })}
      </select>
      <Icon name="down" size={14} />
    </span>
  );
}

export type Tone = "neutral" | "good" | "warn" | "accent" | "alert" | "muted";
export const Tag = ({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) => (
  <span className={cx("tag", `tag-${tone}`)} title={title}>{children}</span>
);

/** Several on/off choices as toggle chips. */
export function ChoiceChips({ options, value, onChange, label }: { options: (string | [string, string])[]; value: string[]; onChange: (v: string[]) => void; label: string }) {
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((o) => {
        const [v, l] = Array.isArray(o) ? o : [o, o];
        const on = value.includes(v);
        return (
          <button key={v} type="button" className="chip chip-toggle" aria-pressed={on}
                  onClick={() => onChange(on ? value.filter((x) => x !== v) : [...value, v])}>
            {on && <Icon name="check" size={12} />}
            {l}
          </button>
        );
      })}
    </div>
  );
}

const BCP47 = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$/;
/** A list of BCP-47 tags as removable chips plus an input (Enter or comma adds). */
export function LangChips({ value, onChange, known, label, placeholder = "add…" }: { value: string[]; onChange: (v: string[]) => void; known: string[]; label: string; placeholder?: string }) {
  const [draft, setDraft] = useState("");
  const listId = useId();
  const add = () => {
    const v = draft.trim().replace("_", "-");
    setDraft("");
    if (!v || value.includes(v)) return;
    if (!BCP47.test(v)) {
      toast(`${v} isn't a language tag (BCP-47, e.g. en, pt-PT)`, true);
      return;
    }
    onChange([...value, v]);
  };
  return (
    <div className="chips chips-edit">
      {value.map((t) => (
        <span key={t} className="chip" title={langName(t)}>
          {t}
          <button type="button" className="chip-x" aria-label={`Remove ${langName(t)}`}
                  onClick={() => onChange(value.filter((x) => x !== t))}>
            <Icon name="close" size={12} />
          </button>
        </span>
      ))}
      <input className="chip-input" value={draft} list={listId} size={8} placeholder={placeholder}
             aria-label={label} onChange={(e) => setDraft(e.target.value)} onBlur={add}
             onKeyDown={(e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); } }} />
      <datalist id={listId}>{known.map((t) => <option key={t} value={t}>{langName(t)}</option>)}</datalist>
    </div>
  );
}

/** A titled region of a settings page: a heading, a short description, its content. No box. */
export function Section({ title, description, actions, children, id }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; children?: ReactNode; id?: string }) {
  return (
    <section className="section" aria-labelledby={id}>
      <header className="section-head">
        <div>
          <h3 id={id}>{title}</h3>
          {description && <p className="section-desc">{description}</p>}
        </div>
        {actions && <div className="section-actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

type FoldBody = ReactNode | ((open: boolean) => ReactNode); // a function renders what polls only while open

/** A disclosure whose open state this browser remembers, per `id`. */
export function Fold({ id, summary, children, open: initial = false, className }: { id: string; summary: ReactNode; children: FoldBody; open?: boolean; className?: string }) {
  const key = `wavescribe.fold.${id}`;
  const [open, setOpen] = useState(() => {
    try {
      const v = localStorage.getItem(key);
      return v === null ? initial : v === "1";
    } catch {
      return initial;
    }
  });
  return (
    <details className={cx("fold", className)} open={open}
             onToggle={(e) => {
               const now = e.currentTarget.open;
               setOpen(now);
               try { localStorage.setItem(key, now ? "1" : "0"); } catch { /* remembered for this visit only */ }
             }}>
      <summary><Icon name="right" size={14} className="fold-chev" />{summary}</summary>
      <div className="fold-body">{typeof children === "function" ? children(open) : children}</div>
    </details>
  );
}

/** A section's rarely needed controls, folded away until asked for. Closed at first; each section
 * remembers whether it was left open. */
export const Advanced = ({ id, children, label = "Advanced" }: { id: string; children: FoldBody; label?: string }) => (
  <Fold id={`adv.${id}`} summary={label} className="adv">{children}</Fold>
);

export interface Col {
  label: ReactNode;
  num?: boolean;
  width?: string;
}
export function Table({ cols, children, empty = "Nothing yet", caption }: { cols: Col[]; children: ReactNode; empty?: ReactNode; caption?: string }) {
  const rows = Array.isArray(children) ? children.flat().filter(Boolean) : children ? [children] : [];
  return (
    <div className="table-wrap">
      <table className="table">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>{cols.map((c, i) => <th key={i} className={c.num ? "num" : undefined} style={c.width ? { width: c.width } : undefined}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length ? children : <tr><td colSpan={cols.length} className="table-empty">{empty}</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/** A button that asks once more before doing something hard to undo. */
/** A destructive action asked twice. The question stays until it is answered or focus moves
 * elsewhere, so reading it never runs out of time. */
export function ConfirmButton({ children, confirm, onConfirm, icon, size = "md", variant = "ghost" }: { children: ReactNode; confirm: string; onConfirm: () => Promise<unknown> | void; icon?: IconName; size?: "sm" | "md"; variant?: Variant }) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (armed) ref.current?.focus(); }, [armed]);
  if (!armed) return <Button variant={variant} size={size} icon={icon} onClick={() => setArmed(true)}>{children}</Button>;
  return (
    <span className="confirm" onBlur={(e) => { if (!busy && !e.currentTarget.contains(e.relatedTarget as Node)) setArmed(false); }}
          onKeyDown={(e) => { if (e.key === "Escape") setArmed(false); }}>
      <Button ref={ref} variant="danger" size={size} busy={busy}
              onClick={async () => { setBusy(true); try { await onConfirm(); } finally { setBusy(false); setArmed(false); } }}>
        {confirm}
      </Button>
      <Button variant="ghost" size={size} onClick={() => setArmed(false)}>Cancel</Button>
    </span>
  );
}

// -- toasts ----------------------------------------------------------------------------------
interface ToastItem { id: number; text: string; bad: boolean }
const toasts: ToastItem[] = [];
let toastId = 0;
export function toast(text: string, bad = false) {
  const t = { id: ++toastId, text, bad };
  toasts.push(t);
  emit("toast");
  setTimeout(() => {
    toasts.splice(toasts.indexOf(t), 1);
    emit("toast");
  }, bad ? 7000 : 3200);
}
export function Toasts() {
  useTopics("toast");
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={cx("toast", t.bad && "toast-bad")}>
          {t.bad && <Icon name="warn" />}
          {t.text}
        </div>
      ))}
    </div>
  );
}

/** Runs an action, reports failure as a toast and success when `done` is given. */
export async function act<T>(fn: () => Promise<T>, done?: string): Promise<T | undefined> {
  try {
    const r = await fn();
    if (done) toast(done);
    return r;
  } catch (e) {
    toast((e as Error).message, true);
    return undefined;
  }
}

/** A button whose click runs `act`, disabled while it runs. */
export function ActButton({ run, done, ...rest }: ButtonProps & { run: () => Promise<unknown>; done?: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <Button {...rest} busy={busy} onClick={async () => { setBusy(true); try { await act(run, done); } finally { setBusy(false); } }} />
  );
}

/** Closes a popover on Escape or a click outside it, and returns focus to its button. */
export function useDismiss(open: boolean, close: () => void, refs: React.RefObject<HTMLElement | null>[]) {
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!refs.some((r) => r.current?.contains(e.target as Node))) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        (refs[0].current as HTMLElement | null)?.focus();
      }
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
}
