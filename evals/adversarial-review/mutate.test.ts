import { test, expect, afterAll } from "bun:test";
import { $ } from "bun";
import { join } from "node:path";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";

const script = join(import.meta.dir, "../../skills/adversarial-review/scripts/mutate.ts");

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
  const out = (await new Response(proc.stdout).text()) + (await new Response(proc.stderr).text());
  return { code: await proc.exited, out };
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
});

test("a mutant that hangs times out and counts as caught", async () => {
  const dir = await repo(add);
  const hang = [{ name: "hang", file: "add.ts", find: "a + b", replace: "(() => { while (true) {} })()" }];
  const { code, out } = await mutate(dir, hang, "bun test", ["--timeout", "2"]);
  expect(out).toMatch(/TIMEOUT\s+hang/);
  expect(code).toBe(0);
});
