import { test, expect, afterAll, setDefaultTimeout } from "bun:test";
import { $ } from "bun";
import { join } from "node:path";
import { mkdtemp, mkdir, readdir, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";

const script = join(import.meta.dir, "../../skills/adversarial-review/scripts/mutate.ts");

// Each case runs git, the script and the suite at least twice; bun's 5 s
// default would fail a slow runner before an assertion could.
setDefaultTimeout(30_000);

// Every temp dir a test makes, removed at the end. Honours TMPDIR, for
// sandboxes where /tmp isn't writable.
const made: string[] = [];
const tempDir = async () => {
  const dir = await mkdtemp(join(tmpdir(), "mutate-test-"));
  made.push(dir);
  return dir;
};
afterAll(() => Promise.all(made.map((d) => rm(d, { recursive: true, force: true }))));

// A throwaway git repo with one function and one test, so each case runs the
// script end to end exactly as the reviewer would.
async function repo(files: Record<string, string>, gitignore = "") {
  const dir = await tempDir();
  for (const [name, content] of Object.entries(files)) await Bun.write(join(dir, name), content);
  if (gitignore) await Bun.write(join(dir, ".gitignore"), gitignore);
  await $`git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm init`.cwd(dir);
  return dir;
}

async function mutate(dir: string, mutants: unknown, cmd = "bun test", extra: string[] = []) {
  const json = join(await tempDir(), "mutants.json");
  await Bun.write(json, JSON.stringify(mutants));
  const proc = Bun.spawn(["bun", script, json, "--cmd", cmd, ...extra], {
    cwd: dir,
    stdout: "pipe",
    stderr: "pipe",
  });
  const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
  return { code: await proc.exited, out: stdout + stderr };
}

const add = {
  "add.ts": "export const add = (a: number, b: number) => a + b;\n",
  "add.test.ts": 'import { test, expect } from "bun:test";\nimport { add } from "./add";\ntest("adds", () => expect(add(2, 3)).toBe(5));\n',
};

test("reports caught, survived and unapplicable mutants", async () => {
  const dir = await repo(add);
  const { code, out } = await mutate(dir, [
    { name: "minus", file: "add.ts", find: "a + b", replace: "a - b" },
    { name: "comment only", file: "add.ts", find: "export const", replace: "/* x */ export const" },
    { name: "missing", file: "add.ts", find: "nope", replace: "x" },
  ]);
  expect(out).toMatch(/caught\s+minus/);
  expect(out).toMatch(/SURVIVED\s+comment only/);
  expect(out).toMatch(/NOT APPLIED\s+missing/);
  expect(code).toBe(1);
});

test("refuses to run when the unmutated tests fail in a copy, instead of calling every mutant caught", async () => {
  // The test reads an ignored file, which the copy won't have, so the tests
  // fail before any mutant is applied.
  const dir = await repo(
    {
      ...add,
      "add.test.ts":
        'import { test, expect } from "bun:test";\nimport { add } from "./add";\ntest("adds", async () => { expect(await Bun.file(".env").text()).toBe("ok"); expect(add(2, 3)).toBe(5); });\n',
    },
    ".env\n",
  );
  await Bun.write(join(dir, ".env"), "ok");
  expect((await $`bun test`.cwd(dir).nothrow()).exitCode).toBe(0); // passes in place
  const { code, out } = await mutate(dir, [{ name: "minus", file: "add.ts", find: "a + b", replace: "a - b" }]);
  expect(code).toBe(2);
  expect(out).toContain("unmutated");
  expect(out).not.toMatch(/caught\s+minus/);
});

test("runs only the tests changed in HEAD, or the full suite with --all", async () => {
  // add.test.ts would catch the mutant, but HEAD only adds other.test.ts, so the
  // targeted run misses it and --all catches it.
  const dir = await repo(add);
  await Bun.write(join(dir, "other.test.ts"), 'import { test, expect } from "bun:test";\ntest("other", () => expect(1).toBe(1));\n');
  await $`git add -A && git -c user.name=t -c user.email=t@t commit -qm other`.cwd(dir);
  const minus = [{ name: "minus", file: "add.ts", find: "a + b", replace: "a - b" }];
  const targeted = await mutate(dir, minus);
  expect(targeted.out).toContain("Tests changed in HEAD: other.test.ts");
  expect(targeted.out).toMatch(/SURVIVED\s+minus/);
  const all = await mutate(dir, minus, "bun test", ["--all"]);
  expect(all.out).toContain("Running the full suite");
  expect(all.out).toMatch(/caught\s+minus/);
});

test("a bad mutant is reported as not applied and the others still run", async () => {
  const dir = await repo(add);
  const { code, out } = await mutate(dir, [
    { name: "no such file", file: "nope.ts", find: "x", replace: "y" },
    { name: "no find", file: "add.ts", replace: "y" },
    { name: "twice", file: "add.ts", find: "b", replace: "c" },
    { name: "minus", file: "add.ts", find: "a + b", replace: "a - b" },
  ]);
  expect(out).toMatch(/NOT APPLIED\s+no such file: no such file: nope.ts/);
  expect(out).toMatch(/NOT APPLIED\s+no find/);
  expect(out).toMatch(/NOT APPLIED\s+twice: "find" matched \d+ times/);
  expect(out).toMatch(/caught\s+minus/);
  expect(code).toBe(1);
});

test("exits 3, not 1, when the script itself can't run, so it isn't read as a survivor", async () => {
  const dir = await repo(add);
  const minus = [{ name: "minus", file: "add.ts", find: "a + b", replace: "a - b" }];
  const badTmp = await mutate(dir, minus, "bun test", ["--tmp", join(dir, "no-such-dir")]);
  expect(badTmp.code).toBe(3);
  expect(badTmp.out).toContain("pass a writable --tmp");
  const notArray = await mutate(dir, { name: "minus" });
  expect(notArray.code).toBe(3);
  expect(notArray.out).toContain("expected a JSON array");
  const badTimeout = await mutate(dir, minus, "bun test", ["--timeout", "abc"]);
  expect(badTimeout.code).toBe(3);
  expect(badTimeout.out).toContain("--timeout");
  const huge = await mutate(dir, minus, "bun test", ["--timeout", "Infinity"]);
  expect(huge.code).toBe(3);
  const noCommits = await tempDir();
  await Bun.write(join(noCommits, "add.ts"), add["add.ts"]);
  await $`git init -q`.cwd(noCommits);
  const unborn = await mutate(noCommits, minus);
  expect(unborn.code).toBe(3);
  expect(unborn.out).toContain("could not run");
});

test("copies names git would quote and symlinks, and leaves out nested repos", async () => {
  const dir = await repo({
    ...add,
    "café.ts": 'export const name = "café";\n',
    "café.test.ts": 'import { test, expect } from "bun:test";\nimport { name } from "./café";\nimport { add } from "./link";\ntest("name", () => { expect(name).toBe("café"); expect(add(1, 1)).toBe(2); });\n',
  });
  await symlink("add.ts", join(dir, "link.ts"));
  await symlink("no-such-file", join(dir, "dangling"));
  await $`git init -q nested && git -C nested -c user.name=t -c user.email=t@t commit -q --allow-empty -m n`.cwd(dir);
  await $`git add -A && git -c user.name=t -c user.email=t@t commit -qm links`.cwd(dir);
  const { code, out } = await mutate(dir, [{ name: "accent", file: "café.ts", find: '"café"', replace: '"cafe"' }], "bun test", ["--all"]);
  expect(out).toMatch(/caught\s+accent/);
  expect(code).toBe(0);
});

test("writes $ patterns in the replace text as is", async () => {
  // Read as a replacement pattern, "$&" would put "a + b" back: no mutation, so
  // it would survive. Written as is, it breaks the code and is caught.
  const dir = await repo(add);
  const { out } = await mutate(dir, [{ name: "dollar", file: "add.ts", find: "a + b", replace: "$&" }]);
  expect(out).toMatch(/caught\s+dollar/);
});

test("a timeout kills the whole test process tree", async () => {
  // The runner is a grandchild of sh: killing only sh would leave it holding
  // the output pipes until it exits on its own.
  const dir = await repo(add);
  const hang = [{ name: "hang", file: "add.ts", find: "a + b", replace: "(() => { while (true) {} })()" }];
  const start = Date.now();
  const { out } = await mutate(dir, hang, "bun test && true", ["--timeout", "2"]);
  expect(out).toMatch(/TIMEOUT\s+hang/);
  expect(Date.now() - start).toBeLessThan(20_000);
});

test("a mutant that hangs times out and counts as caught", async () => {
  const dir = await repo(add);
  const hang = [{ name: "hang", file: "add.ts", find: "a + b", replace: "(() => { while (true) {} })()" }];
  const { code, out } = await mutate(dir, hang, "bun test", ["--timeout", "2"]);
  expect(out).toMatch(/TIMEOUT\s+hang/);
  expect(code).toBe(0);
});

test("a suite that passes but leaves a process running is a pass, not a timeout", async () => {
  // The background sleep holds the output pipes open after sh exits.
  const dir = await repo(add);
  const comment = [{ name: "comment only", file: "add.ts", find: "export const", replace: "/* x */ export const" }];
  const start = Date.now();
  const { out } = await mutate(dir, comment, "bun test; sleep 30 &", ["--all", "--timeout", "10"]);
  expect(out).toMatch(/SURVIVED\s+comment only/);
  expect(Date.now() - start).toBeLessThan(10_000);
});

test("symlinks out of the repo are followed for reading but never written through", async () => {
  const outside = await tempDir();
  await Bun.write(join(outside, "lib.ts"), "export const one = 1;\n");
  const dir = await repo({
    ...add,
    "one.test.ts": 'import { test, expect } from "bun:test";\nimport { one } from "./base";\ntest("one", () => expect(one).toBe(1));\n',
  });
  await symlink(join(outside, "lib.ts"), join(dir, "base.ts")); // copied: the copy can't reach it by a relative path
  await symlink(outside, join(dir, "shared")); // linked by absolute path
  await $`git add -A && git -c user.name=t -c user.email=t@t commit -qm links`.cwd(dir);
  const { out } = await mutate(dir, [{ name: "through", file: "shared/lib.ts", find: "= 1", replace: "= 2" }], "bun test", ["--all"]);
  expect(out).toMatch(/NOT APPLIED\s+through: shared\/lib.ts is behind a symlink out of the repo/);
  expect(await Bun.file(join(outside, "lib.ts")).text()).toBe("export const one = 1;\n");
});

test("an interrupt kills the running tests and removes the copy", async () => {
  const dir = await repo(add);
  const tmp = join(await tempDir(), "copies");
  await mkdir(tmp);
  const json = join(await tempDir(), "mutants.json");
  await Bun.write(json, "[]");
  const marker = `sleep ${3000 + Math.floor(Math.random() * 1000)}`;
  const proc = Bun.spawn(["bun", script, json, "--cmd", marker, "--all", "--tmp", tmp], { cwd: dir, stdout: "ignore", stderr: "ignore" });
  await Bun.sleep(1500);
  // the baseline's sh, not this script, whose own argv has the marker too
  expect((await $`pgrep -f ${`^sh -c ${marker}`}`.nothrow().quiet()).exitCode).toBe(0);
  proc.kill("SIGTERM");
  expect(await proc.exited).toBe(3);
  await Bun.sleep(200);
  expect((await $`pgrep -f ${marker}`.nothrow().quiet()).exitCode).toBe(1);
  expect(await readdir(tmp)).toEqual([]);
});

test("a link into the repo by another path points into the copy", async () => {
  // alias/add.ts is add.ts reached through a link to the repo: copied as a
  // plain file, the only test would read the unmutated original.
  const dir = await repo({
    "add.ts": add["add.ts"],
    "via.test.ts": 'import { test, expect } from "bun:test";\nimport { add } from "./via";\ntest("via", () => expect(add(2, 3)).toBe(5));\n',
  });
  const alias = join(await tempDir(), "alias");
  await symlink(dir, alias);
  await symlink(join(alias, "add.ts"), join(dir, "via.ts"));
  await $`git add -A && git -c user.name=t -c user.email=t@t commit -qm via`.cwd(dir);
  const { out } = await mutate(dir, [{ name: "minus", file: "add.ts", find: "a + b", replace: "a - b" }]);
  expect(out).toMatch(/caught\s+minus/);
});

test("a file the copy doesn't have is reported as missing, and a ..name is in the repo", async () => {
  const dir = await repo(
    {
      ...add,
      "..config/flags.ts": "export const on = true;\n",
      "flags.test.ts": 'import { test, expect } from "bun:test";\nimport { on } from "./..config/flags";\ntest("on", () => expect(on).toBe(true));\n',
    },
    "gen.ts\n",
  );
  await Bun.write(join(dir, "gen.ts"), "export const g = 1;\n");
  const { out } = await mutate(
    dir,
    [
      { name: "ignored", file: "gen.ts", find: "= 1", replace: "= 2" },
      { name: "dots", file: "..config/flags.ts", find: "true", replace: "false" },
    ],
    "bun test",
    ["--all"],
  );
  expect(out).toMatch(/NOT APPLIED\s+ignored: gen.ts is not in the copy/);
  expect(out).toMatch(/caught\s+dots/);
});

test("a link to its own directory is copied as one", async () => {
  const dir = await repo(add);
  await symlink(".", join(dir, "self"));
  await $`git add -A && git -c user.name=t -c user.email=t@t commit -qm self`.cwd(dir);
  // a path, so the runner doesn't walk self/self/...
  const { code, out } = await mutate(dir, [{ name: "minus", file: "add.ts", find: "a + b", replace: "a - b" }], "bun test ./add.test.ts", ["--all"]);
  expect(out).toMatch(/caught\s+minus/);
  expect(code).toBe(0);
});

test("a link to a link out of the repo still points at its neighbour", async () => {
  // a.ts -> b.ts -> outside: resolved all the way, a.ts would be its own copy
  // and a mutant on b.ts would never reach the test.
  const outside = await tempDir();
  await Bun.write(join(outside, "impl.ts"), add["add.ts"]);
  const dir = await repo({
    "a.test.ts": 'import { test, expect } from "bun:test";\nimport { add } from "./a";\ntest("adds", () => expect(add(2, 3)).toBe(5));\n',
  });
  await symlink(join(outside, "impl.ts"), join(dir, "b.ts"));
  await symlink("b.ts", join(dir, "a.ts"));
  await $`git add -A && git -c user.name=t -c user.email=t@t commit -qm links`.cwd(dir);
  const { out } = await mutate(dir, [{ name: "minus", file: "b.ts", find: "a + b", replace: "a - b" }], "bun test", ["--all"]);
  expect(out).toMatch(/caught\s+minus/);
  expect(await Bun.file(join(outside, "impl.ts")).text()).toBe(add["add.ts"]);
});
