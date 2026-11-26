// A small topic-based store: modules mutate plain state and emit the topics that changed;
// components subscribe to the topics they read. Live text arrives several times a second, so a
// column re-renders only for its own station's topic.
import { useCallback, useRef, useSyncExternalStore } from "react";

type Listener = () => void;
const listeners = new Map<string, Set<Listener>>();
const versions = new Map<string, number>();

export function emit(...topics: string[]) {
  for (const t of topics) {
    versions.set(t, (versions.get(t) || 0) + 1);
    listeners.get(t)?.forEach((l) => l());
  }
}

function subscribe(topics: string[], l: Listener) {
  for (const t of topics) {
    let set = listeners.get(t);
    if (!set) listeners.set(t, (set = new Set()));
    set.add(l);
  }
  return () => topics.forEach((t) => listeners.get(t)?.delete(l));
}

/** Re-renders the caller whenever one of the topics is emitted; returns a version string. */
export function useTopics(...topics: string[]): string {
  const key = topics.join("\u0000");
  const ref = useRef(topics);
  ref.current = topics;
  const sub = useCallback((l: Listener) => subscribe(ref.current, l), [key]);
  const snap = useCallback(() => ref.current.map((t) => versions.get(t) || 0).join("."), [key]);
  return useSyncExternalStore(sub, snap);
}
