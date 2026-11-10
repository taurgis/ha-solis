import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageJson = JSON.parse(
  await readFile(path.join(repoRoot, "package.json"), "utf8"),
);
const tagName = `v${packageJson.version}`;

const runGit = (...args) => execFileAsync("git", args, { cwd: repoRoot });

try {
  await runGit("rev-parse", "--verify", `refs/tags/${tagName}`);
  console.log(`Tag ${tagName} already exists; skipping.`);
  process.exit(0);
} catch {
  // The tag is not present yet.
}

await runGit("tag", "-a", tagName, "-m", `Release ${tagName}`);
await runGit("push", "origin", `refs/tags/${tagName}`);
console.log(`Created and pushed ${tagName}`);
