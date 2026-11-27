// The page's command hints say the command as it is typed in this install (/api/state's
// invocation), and as in a Wavescribe folder when the server doesn't say.
import assert from "node:assert/strict";
import { test } from "node:test";
import { T } from "../src/reading/strings.ts";

test("the read token's command, as this install types it", () => {
  assert.equal(T.tokenBody.command("wavescribe"), "wavescribe token make");
  assert.equal(T.tokenBody.command("uv run wavescribe"), "uv run wavescribe token make");
});

test("a server from before the field: as in a Wavescribe folder", () => {
  for (const missing of [undefined, null, "", "  ", 3]) {
    assert.equal(T.tokenBody.command(missing), "uv run wavescribe token make", String(missing));
  }
});
