// Icons are Lucide (ISC; a few derived from Feather, MIT): one consistent, well-drawn set on a
// 24px grid, scaled to 16px with currentColor strokes. The page's own mark is drawn below.
import type { LucideIcon, LucideProps } from "lucide-react";
import {
  ALargeSmall, Activity, ArrowDown, ArrowLeft, ArrowUp, Bell, Box, Check, ChevronDown, ChevronLeft,
  ChevronRight, ChevronUp, Code, Columns3, Copy, Download, Ellipsis, ExternalLink, Eye, EyeOff, Globe, HardDrive, History, Info,
  Keyboard, Languages, MessageCircleQuestionMark, PanelRight, Pause, Pencil, Play, Plus, Minus, Power, Radio,
  RadioTower, RefreshCw, ScanSearch, ScrollText, Search, Send, Settings, ShieldCheck, Square, Trash2, TriangleAlert, Volume2, VolumeX, X,
} from "lucide-react";

const I = {
  search: Search,
  ask: MessageCircleQuestionMark,
  play: Play,
  pause: Pause,
  stop: Square,
  close: X,
  plus: Plus,
  minus: Minus,
  check: Check,
  down: ChevronDown,
  up: ChevronUp,
  right: ChevronRight,
  left: ChevronLeft,
  back: ArrowLeft,
  arrowDown: ArrowDown,
  arrowUp: ArrowUp,
  gear: Settings,
  type: ALargeSmall,
  bell: Bell,
  panel: PanelRight,
  speaker: Volume2,
  mute: VolumeX,
  warn: TriangleAlert,
  info: Info,
  copy: Copy,
  external: ExternalLink,
  trash: Trash2,
  more: Ellipsis,
  refresh: RefreshCw,
  download: Download,
  power: Power,
  edit: Pencil,
  globe: Globe,
  eye: Eye,
  eyeOff: EyeOff,
  antenna: RadioTower,
  keyboard: Keyboard,
  live: Radio,
  columns: Columns3,
  history: History,
  pulse: Activity,
  cube: Box,
  send: Send,
  code: Code,
  scan: ScanSearch,
  languages: Languages,
  storage: HardDrive,
  shield: ShieldCheck,
  summary: ScrollText,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof I;

export function Icon({ name, size = 16, ...rest }: { name: IconName; size?: number } & LucideProps) {
  const C = I[name];
  return <C size={size} strokeWidth={2} aria-hidden="true" focusable="false" {...rest} />;
}

/** The logo: radio waves that run on as lines of text; the dot is the word being written. Below
 * 24 px it is drawn with two heavier rings, so it doesn't fill in. */
export function Mark({ size = 20 }: { size?: number }) {
  const small = size <= 24;
  return (
    <svg width={size} height={size} viewBox={small ? "40 40 432 432" : "60 60 392 392"} aria-hidden="true" focusable="false">
      <g fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth={small ? 38 : 24}>
        {small ? (
          <>
            <path d="M380 214 H206 A42 42 0 0 0 206 298 H430" />
            <path d="M430 130 H206 A126 126 0 0 0 206 382 H318" />
          </>
        ) : (
          <>
            <path d="M428 229 H226 A27 27 0 0 0 226 283 H404" />
            <path d="M388 175 H226 A81 81 0 0 0 226 337 H428" />
            <path d="M428 121 H226 A135 135 0 0 0 226 391 H330" />
          </>
        )}
      </g>
      <circle cx={small ? 373.1 : 364.8} cy={small ? 382 : 391} r={small ? 19 : 12} fill="var(--accent)" />
    </svg>
  );
}
