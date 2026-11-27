// A refusal from the server, said in a sentence the page writes, with what can be done about it as
// buttons. The server's own words go under Details: they name ids and settings, which help
// whoever reads the logs and nobody else.
import type { ReactNode } from "react";
import { technical } from "./catches";

export function Refusal({ said, raw, children }: { said?: ReactNode; raw: string; children?: ReactNode }) {
  const plain = !technical(raw);
  return (
    <div className="m-trouble" role="alert">
      <p>{said ?? (plain ? raw : "That can't be done now.")}</p>
      {children && <div className="m-actions">{children}</div>}
      {(said != null || !plain) && (
        <details className="m-why">
          <summary>Details</summary>
          <p className="mono">{raw}</p>
        </details>
      )}
    </div>
  );
}
