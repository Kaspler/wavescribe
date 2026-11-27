// Where reading this Wavescribe takes a read token (it is reached through a proxy) and the page
// has none, or the one it kept was revoked: one plain screen that asks for it, instead of a page
// whose every part fails.
import { type FormEvent, useState } from "react";
import { tryToken, wanted } from "../lib/auth";
import { useTopics } from "../lib/store";
import { Button, Field, Input } from "../components/ui";
import { T } from "./strings";

export function TokenGate() {
  useTopics("auth");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState<string | null>(wanted() === "refused" ? T.tokenGone : null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const t = token.trim();
    if (!t) return;
    setBusy(true);
    try {
      // Accepted: the page starts again with it, so every part reads with the token from the start.
      if (await tryToken(t)) location.reload();
      else setWhy(T.tokenWrong);
    } catch (err) {
      setWhy(`${T.cantConnect}: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="gate" id="main" tabIndex={-1}>
      <form className="gate-box" onSubmit={submit} aria-labelledby="gate-h">
        <h1 id="gate-h">{T.tokenTitle}</h1>
        <p>{T.tokenBody}</p>
        <Field id="gate-token" label={T.tokenLabel}>
          <Input id="gate-token" type="password" value={token} autoFocus autoComplete="off" spellCheck={false}
                 aria-invalid={why ? true : undefined} aria-describedby={why ? "gate-why" : undefined}
                 onChange={(e) => setToken(e.target.value)} />
        </Field>
        {why && <p id="gate-why" className="gate-why" role="alert">{why}</p>}
        <Button type="submit" variant="primary" busy={busy} disabled={!token.trim()}>{T.tokenGo}</Button>
      </form>
    </main>
  );
}
