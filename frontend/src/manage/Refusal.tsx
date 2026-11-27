// A refusal from the server, with what can be done about it as buttons. Its sentence is shown as
// the server wrote it; where the page says it in its own words for a refusal it knows by its code
// (`said`), the server's sentence goes under Details.
import type { ReactNode } from "react";

export function Refusal({ said, raw, children }: { said?: ReactNode; raw: string; children?: ReactNode }) {
  return (
    <div className="m-trouble" role="alert">
      <p>{said ?? raw}</p>
      {children && <div className="m-actions">{children}</div>}
      {said != null && (
        <details className="m-why">
          <summary>Details</summary>
          <p className="mono">{raw}</p>
        </details>
      )}
    </div>
  );
}
