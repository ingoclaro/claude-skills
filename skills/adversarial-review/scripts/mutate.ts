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
// files, symlinks within the repo kept as symlinks, submodules and nested repos
// left out, node_modules symlinked), with each mutated file restored after its run, so
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
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { rmSync } from "node:fs";
import { mkdtemp, mkdir, rm, symlink, copyFile, lstat, readlink, realpath, stat } from "node:fs/promises";

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
// setTimeout fires at once past 2^31-1 ms, so that's the cap.
if (!(timeoutSec > 0 && timeoutSec <= 2147483))
  fail("reading --timeout", `expected a number of seconds up to 2147483, got ${flag("--timeout", "60")}`);
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

const inside = (base: string, path: string) => {
  const rel = relative(base, path);
  return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel);
};

// A directory is a submodule or a nested repo: left out. A symlink to a path in
// the repo is recreated relative, so it points into the copy, even when it
// dangles. One that leaves the repo can't be followed from the copy's place:
// a file it points to is copied, anything else is linked by absolute path.
// Copies run in batches, so a large repo doesn't run out of file descriptors.
async function copyEntry(dir: string, f: string) {
  const src = join(root, f);
  const dst = join(dir, f);
  const st = await lstat(src);
  if (st.isDirectory()) return;
  await mkdir(dirname(dst), { recursive: true });
  if (!st.isSymbolicLink()) return copyFile(src, dst);
  const target = resolve(dirname(src), await readlink(src));
  if (inside(root, target)) return symlink(relative(dirname(src), target), dst);
  if ((await stat(target).catch(() => null))?.isFile()) return copyFile(target, dst);
  await symlink(target, dst);
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

// The copy and the running test group, for the interrupt handler below.
let copyDir: string | undefined;
let group: number | undefined;
const killGroup = (pid: number) => {
  try {
    process.kill(-pid, "SIGKILL");
  } catch {}
};
// The test group is detached, so an interrupt to this script doesn't reach it:
// kill it and remove the copy here, or a hung mutant keeps running.
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as const)
  process.on(sig, () => {
    if (group) killGroup(group);
    if (copyDir) rmSync(copyDir, { recursive: true, force: true });
    process.exit(3);
  });

// The test command runs in its own process group, and a timeout kills the
// whole group: killing only `sh` would leave the runner holding the pipes open.
// The result is `sh`'s exit; anything it leaves running (a server, a watcher)
// is killed then, so its open pipes can't turn a pass into a timeout. Both
// streams are read as they arrive, so a full stderr can't block the run.
function runTests(dir: string): Promise<{ code: number; timedOut: boolean; output: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn("sh", ["-c", testCmd], { cwd: dir, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    group = child.pid;
    let output = "";
    let exitCode: number | undefined;
    let timedOut = false;
    child.stdout.setEncoding("utf8").on("data", (d) => (output += d));
    child.stderr.setEncoding("utf8").on("data", (d) => (output += d));
    const timer = setTimeout(() => {
      timedOut = true;
      killGroup(child.pid!);
    }, timeoutSec * 1000);
    child.on("exit", (code) => {
      clearTimeout(timer);
      exitCode = code ?? 1;
      killGroup(child.pid!);
    });
    child.on("error", (e) => (clearTimeout(timer), (group = undefined), reject(e)));
    child.on("close", () => ((group = undefined), resolve({ code: exitCode ?? 1, timedOut, output })));
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
  // Behind a symlink out of the repo, writing the mutant would change the real file.
  const real = await realpath(join(dir, m.file)).catch(() => null);
  if (!real || !inside(await realpath(dir), real))
    return { name, outcome: "NOT APPLIED", detail: `${m.file} is behind a symlink out of the repo` };
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
copyDir = dir;
const results: Result[] = [];
let crashed: unknown;
try {
  // Baseline: the tests must pass unmutated in the copy, or no result means anything.
  const { code, timedOut, output } = await runTests(dir);
  if (timedOut) throw new Error(`the unmutated tests gave no result in ${timeoutSec}s (raise --timeout)`);
  if (code !== 0) {
    console.error(`The unmutated tests fail in a copy of the repo (exit ${code}), so every mutant would look caught: ${testCmd}`);
    console.error("The copy has tracked and untracked files plus node_modules; ignored files (.env, .venv, build output) and the contents of submodules and nested repos are missing.");
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
