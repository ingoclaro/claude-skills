// Plants bugs one at a time in a throwaway copy of the repo and runs the tests
// against each, to show which bugs the suite misses. Used by the adversarial
// reviewer so it doesn't rebuild this every review.
//
//   bun <skill dir>/scripts/mutate.ts <mutants.json> [--cmd "bun test"] [--all] [--timeout 60] [--tmp <dir>]
//
// mutants.json: [{ "name": "...", "file": "src/x.ts", "find": "...", "replace": "..." }]
// "find" must match exactly once. Each mutant runs only the test files added or
// changed in HEAD, appended to --cmd, since those are the tests under review.
// --all, or a commit that touches no test files, runs --cmd as given (needed
// when the runner takes packages rather than files, like `go test ./...`).
// Mutants run one at a time in a single copy of the repo (tracked + untracked
// files, symlinks kept as symlinks, submodules and nested repos left out,
// node_modules symlinked), with each mutated file restored after its run, so
// the working tree is never touched. The copy goes under --tmp (default: the
// system temp dir), for sandboxes where /tmp isn't writable.
// Exit codes: 0 every mutant caught. 1 any mutant survived or couldn't be
// applied (a bad "file" or "find" is reported per mutant, the rest still run).
// 2 the unmutated tests already fail in the copy (an ignored file such as .env
// or .venv is missing there): every mutant would look caught. 3 the script
// itself couldn't run (bad mutants.json or --timeout, temp dir not writable, not
// a repo or no commits, the unmutated tests timing out).

import { $ } from "bun";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdtemp, mkdir, rm, symlink, copyFile, lstat, readlink } from "node:fs/promises";

type Mutant = { name: string; file: string; find: string; replace: string };
type Result = { name: string; outcome: "caught" | "SURVIVED" | "NOT APPLIED" | "TIMEOUT"; detail: string };

const args = process.argv.slice(2);
const flag = (name: string, fallback: string) => {
  const i = args.indexOf(name);
  return i === -1 ? fallback : args[i + 1]!;
};
const mutantsPath = args[0];
if (!mutantsPath || mutantsPath.startsWith("--")) {
  console.error('usage: bun <skill dir>/scripts/mutate.ts <mutants.json> [--cmd "bun test"] [--all] [--timeout 60] [--tmp <dir>]');
  process.exit(3);
}
// Anything that stops the script itself exits 3, so it can't be read as exit 1
// ("a mutant survived").
const fail = (what: string, e: unknown): never => {
  console.error(`mutate.ts could not run: ${what}: ${e instanceof Error ? e.message : e}`);
  process.exit(3);
};
const baseCmd = flag("--cmd", "bun test");
const timeoutSec = Number(flag("--timeout", "60"));
if (!(timeoutSec > 0)) fail("reading --timeout", `expected a number of seconds, got ${flag("--timeout", "60")}`);
const tmpRoot = flag("--tmp", tmpdir());
const mutants: Mutant[] = await Bun.file(mutantsPath).json().catch((e) => fail(`reading ${mutantsPath}`, e));
if (!Array.isArray(mutants)) fail(`reading ${mutantsPath}`, "expected a JSON array of mutants");
const root = (await $`git rev-parse --show-toplevel`.quiet().text().catch((e) => fail("finding the repo root", e))).trim();

// The tests added or changed in HEAD: the new tests are what the review is checking.
const testFile = /\.(test|spec)\.[cm]?[jt]sx?$|(^|\/)test_[^/]*\.py$|_test\.py$/;
const changedTests = args.includes("--all")
  ? []
  : (await $`git -C ${root} diff-tree --no-commit-id --name-only -r --root --diff-filter=AM -z HEAD`.quiet().text().catch((e) => fail("listing the tests changed in HEAD", e)))
      .split("\0")
      .filter((f) => testFile.test(f));
const quote = (f: string) => `'${f.replaceAll("'", `'\\''`)}'`;
const testCmd = changedTests.length ? `${baseCmd} ${changedTests.map(quote).join(" ")}` : baseCmd;
console.log(changedTests.length ? `Tests changed in HEAD: ${changedTests.join(", ")}` : `Running the full suite: ${baseCmd}`);
// -z: without it, git quotes names with non-ASCII characters.
const files = (await $`git -C ${root} ls-files -co --exclude-standard -z`.quiet().text().catch((e) => fail("listing the repo's files", e)))
  .split("\0")
  .filter(Boolean);

// A symlink is recreated as one, even when it dangles. A directory is a
// submodule or a nested repo: left out. Copies run in batches, so a large repo
// doesn't run out of file descriptors.
async function copyEntry(dir: string, f: string) {
  const stat = await lstat(join(root, f));
  if (stat.isDirectory()) return;
  await mkdir(join(dir, f, ".."), { recursive: true });
  if (stat.isSymbolicLink()) await symlink(await readlink(join(root, f)), join(dir, f));
  else await copyFile(join(root, f), join(dir, f));
}

async function copyRepo(): Promise<string> {
  // Some sandboxes let mkdtemp "succeed" in a dir they then hide, so prove the copy is readable.
  const dir = await mkdtemp(join(tmpRoot, "mutant-"))
    .then(async (d) => (await Bun.write(join(d, ".probe"), "ok"), (await Bun.file(join(d, ".probe")).text()) === "ok" ? d : Promise.reject("not readable back")))
    .catch((e) => fail(`using a temp dir under ${tmpRoot} (pass a writable --tmp)`, e));
  for (let i = 0; i < files.length; i += 64) await Promise.all(files.slice(i, i + 64).map((f) => copyEntry(dir, f)));
  await symlink(join(root, "node_modules"), join(dir, "node_modules")).catch(() => {});
  return dir;
}

// The test command runs in its own process group, and a timeout kills the
// whole group: killing only `sh` would leave the runner holding the pipes open.
// Both streams are read as they arrive, so a full stderr can't block the run.
function runTests(dir: string): Promise<{ code: number; timedOut: boolean; output: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn("sh", ["-c", testCmd], { cwd: dir, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    let timedOut = false;
    child.stdout.on("data", (d) => (output += d));
    child.stderr.on("data", (d) => (output += d));
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        process.kill(-child.pid!, "SIGKILL");
      } catch {}
    }, timeoutSec * 1000);
    child.on("error", (e) => (clearTimeout(timer), reject(e)));
    child.on("close", (code) => (clearTimeout(timer), resolve({ code: code ?? 1, timedOut, output })));
  });
}

async function run(m: Mutant, dir: string): Promise<Result> {
  const name = String(m?.name ?? "(unnamed)");
  if (typeof m?.file !== "string" || typeof m.find !== "string" || typeof m.replace !== "string")
    return { name, outcome: "NOT APPLIED", detail: 'needs string "file", "find" and "replace"' };
  const source = await Bun.file(join(root, m.file)).text().catch(() => null);
  if (source === null) return { name, outcome: "NOT APPLIED", detail: `no such file: ${m.file}` };
  const count = source.split(m.find).length - 1;
  if (count !== 1) return { name, outcome: "NOT APPLIED", detail: `"find" matched ${count} times in ${m.file}` };
  try {
    // A function, so "$&" or "$$" in the replace text is written as is.
    await Bun.write(join(dir, m.file), source.replace(m.find, () => m.replace));
    const { code, timedOut, output } = await runTests(dir);
    if (timedOut) return { name, outcome: "TIMEOUT", detail: `no result in ${timeoutSec}s (counts as caught)` };
    const failed = output.split("\n").filter((l) => l.startsWith("(fail)")).map((l) => l.replace(/\s*\[[\d.]+m?s\]$/, ""));
    return code === 0
      ? { name, outcome: "SURVIVED", detail: "all tests passed" }
      : { name, outcome: "caught", detail: failed.slice(0, 3).join("; ") || `exit ${code}` };
  } finally {
    await Bun.write(join(dir, m.file), source);
  }
}

// One copy, mutants run one at a time.
const dir = await copyRepo().catch((e) => fail("copying the repo", e));
const results: Result[] = [];
let crashed: unknown;
try {
  // Baseline: the tests must pass unmutated in the copy, or no result means anything.
  const { code, timedOut, output } = await runTests(dir);
  if (timedOut) throw new Error(`the unmutated tests gave no result in ${timeoutSec}s (raise --timeout)`);
  if (code !== 0) {
    console.error(`The unmutated tests fail in a copy of the repo (exit ${code}), so every mutant would look caught: ${testCmd}`);
    console.error("The copy has tracked and untracked files plus node_modules; ignored files (.env, .venv, build output) are missing.");
    console.error(`Fix the test command with --cmd (or pass --all if the runner can't take file paths), or make the tests runnable from a clean checkout. Last output:\n${output.split("\n").slice(-15).join("\n")}`);
    process.exitCode = 2;
  } else {
    for (const m of mutants) results.push(await run(m, dir));
  }
} catch (e) {
  crashed = e;
} finally {
  await rm(dir, { recursive: true, force: true }).catch(() => {});
}
if (crashed) fail("running the tests", crashed);
if (process.exitCode === 2) process.exit(2);

const order = { SURVIVED: 0, "NOT APPLIED": 1, TIMEOUT: 2, caught: 3 };
results.sort((a, b) => order[a.outcome] - order[b.outcome]);
for (const r of results) console.log(`${r.outcome.padEnd(11)} ${r.name}: ${r.detail}`);
const bad = results.filter((r) => r.outcome === "SURVIVED" || r.outcome === "NOT APPLIED").length;
console.log(`\n${results.length} mutants, ${results.filter((r) => r.outcome === "caught" || r.outcome === "TIMEOUT").length} caught, ${bad} survived or not applied`);
process.exit(bad ? 1 : 0);
