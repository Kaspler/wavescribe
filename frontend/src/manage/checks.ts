// Health's line on the checks of keyword matches ("Let the model confirm"), from the status's
// llm_rules.verify: counts since Wavescribe started. A plain function, so it can be tested
// without a page.

/** Why a check didn't happen, as rules/llm.py counts it (NOT_RUN). */
const WHY: Record<string, string> = {
  timeout: "timed out",
  failed: "failed",
  queue_full: "model busy",
  no_model: "no model loaded",
};

const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.round(v) : 0);

/** "Word matches the model was asked to confirm, since start: 4 confirmed, 1 rejected, 3 not
 * run (2 timed out, 1 no model loaded), 1 waiting for a retry." Null when every count is 0 and
 * no rule that is on asks for the check (`verifyOn`): nothing to say. */
export function checksLine(verify: unknown, verifyOn: boolean): string | null {
  const v = verify && typeof verify === "object" ? (verify as Record<string, unknown>) : {};
  const notRun = Object.entries(v.not_run && typeof v.not_run === "object" ? (v.not_run as Record<string, unknown>) : {})
    .map(([why, c]) => [why, n(c)] as const)
    .filter(([, c]) => c > 0);
  const confirmed = n(v.confirmed), rejected = n(v.rejected), held = n(v.held);
  const missed = notRun.reduce((sum, [, c]) => sum + c, 0);
  if (!verifyOn && !confirmed && !rejected && !held && !missed) return null;
  const whys = notRun.map(([why, c]) => `${c} ${WHY[why] || why.replace(/_/g, " ")}`).join(", ");
  return `Word matches the model was asked to confirm, since start: ${confirmed} confirmed, ${rejected} rejected, ${missed} not run${whys ? ` (${whys})` : ""}, ${held} waiting for a retry.`;
}
