// Writes the licence notices of everything the bundle contains next to the built page.
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
// What the bundle can contain: the runtime dependencies and theirs, from the lockfile (dev tools
// such as Vite and TypeScript build the page but aren't in it).
const lock = JSON.parse(readFileSync(new URL("../package-lock.json", import.meta.url), "utf8"));
const SHIPPED = [];
const visit = (name) => {
  if (SHIPPED.includes(name)) return;
  SHIPPED.push(name);
  Object.keys(lock.packages[`node_modules/${name}`]?.dependencies || {}).forEach(visit);
};
Object.keys(lock.packages[""].dependencies || {}).forEach(visit);
SHIPPED.sort();
const out = ["Wavescribe's page bundles the following packages.", ""];
for (const name of SHIPPED) {
  const dir = dirname(require.resolve(`${name}/package.json`));
  const meta = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
  out.push(`${"=".repeat(72)}`, `${name} ${meta.version} (${meta.license})`, "");
  out.push(readFileSync(join(dir, "LICENSE"), "utf8").trim(), "");
}
// Vite builds the page but also writes a few lines of its own into it (the helper that preloads
// the lazily loaded chunk), so its notice ships too: its own licence, without the notices of the
// tools it bundles for the build, none of which reach the page.
const vite = dirname(require.resolve("vite/package.json"));
const viteMeta = JSON.parse(readFileSync(join(vite, "package.json"), "utf8"));
const viteLicence = readFileSync(join(vite, "LICENSE.md"), "utf8").split(/^# Licenses of bundled dependencies/m)[0];
out.push(`${"=".repeat(72)}`, `vite ${viteMeta.version} (${viteMeta.license}), its helpers in the bundle`, "");
out.push(viteLicence.replace(/^# .*\n/, "").trim(), "");
writeFileSync(new URL("../../src/wavescribe/web/dist/licenses.txt", import.meta.url), out.join("\n"));
