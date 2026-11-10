import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageJson = JSON.parse(
  await readFile(path.join(repoRoot, "package.json"), "utf8"),
);
const nextVersion = packageJson.version;
const manifestPath = path.join(
  repoRoot,
  "custom_components",
  "solis",
  "manifest.json",
);
const manifestContents = await readFile(manifestPath, "utf8");
const manifestVersionPattern = /^(  "version": ").*(",?)$/m;
if (!manifestVersionPattern.test(manifestContents)) {
  throw new Error("Could not find an integration version in manifest.json");
}
await writeFile(
  manifestPath,
  manifestContents.replace(manifestVersionPattern, `$1${nextVersion}$2`),
  "utf8",
);
console.log(`Synced release version ${nextVersion} to manifest.json`);
