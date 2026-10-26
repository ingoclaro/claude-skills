#!/usr/bin/env bash
# Build the adversarial-review eval fixtures from scratch: one small bun/TS git
# repo per eval, under fixtures/ (gitignored: nested repos would become
# submodules). Each repo's HEAD is the commit under review, and each plants a
# known bug so the grader has ground truth:
#
#   planted-bug     paginate() uses Math.floor for the page count, so the last
#                   partial page is unreachable and an empty list has no page.
#                   Its test only checks page 1 of an exact multiple, so
#                   mutants on the bounds check survive.
#   agreed-decision parseDuration() never validates the whole string, so "5x"
#                   is 0 and "1.5h" is 5h. Bare numbers meaning seconds is
#                   deliberate, and the prompt says so: it must be dismissed,
#                   not reported.
#   dirty-tree      planted-bug plus an uncommitted edit and an untracked file:
#                   the skill must stop before dispatching the reviewer.
#   weak-tests      correct code whose tests miss one rule (rollover to the next
#                   unit); the review must name that gap. See its block below.
#   cache-aliasing  HEAD caches parseFlags(); an older caller mutates the array
#                   it returns, so flags leak between users. Invisible in the
#                   diff alone (see the comment on its block below).
#
# Usage: ./setup_fixtures.sh
set -euo pipefail
cd "$(dirname "$0")"
rm -rf fixtures
mkdir fixtures

# Fixed dates, so a rebuild reproduces the same commit shas.
export GIT_AUTHOR_DATE="2026-01-01T12:00:00Z" GIT_COMMITTER_DATE="2026-01-01T12:00:00Z"
commit() { git -c user.name=Fixture -c user.email=fixture@example.com commit -qm "$1"; }

scaffold() { # <dir> <package name>
  mkdir -p "$1/src"
  cd "$1"
  git init -q -b main
  cat > package.json <<EOF
{ "name": "$2", "private": true, "type": "module", "scripts": { "test": "bun test" } }
EOF
  printf 'node_modules/\n' > .gitignore
  git add -A
  commit "Scaffold $2"
}

paginate() {
  cat > src/paginate.ts <<'EOF'
export type Page<T> = { items: T[]; page: number; totalPages: number };

// Pages are 1-based. The last page holds whatever is left over, and an empty
// list has a single empty page.
export function paginate<T>(items: T[], page: number, size: number): Page<T> {
  if (size <= 0) throw new RangeError("size must be positive");
  const totalPages = Math.floor(items.length / size);
  if (page < 1 || page > totalPages) throw new RangeError(`page ${page} out of range`);
  const start = (page - 1) * size;
  return { items: items.slice(start, start + size), page, totalPages };
}
EOF
  cat > src/paginate.test.ts <<'EOF'
import { test, expect } from "bun:test";
import { paginate } from "./paginate";

test("first page", () => {
  const p = paginate([1, 2, 3, 4, 5, 6], 1, 3);
  expect(p.items).toEqual([1, 2, 3]);
  expect(p.totalPages).toBe(2);
});

test("page 0 is out of range", () => {
  expect(() => paginate([1, 2, 3], 0, 3)).toThrow(RangeError);
});
EOF
  git add -A
  commit "Add paginate()"
}

# planted-bug
( scaffold fixtures/planted-bug pager && paginate )

# agreed-decision
(
  scaffold fixtures/agreed-decision durations
  cat > src/duration.ts <<'EOF'
const UNITS: Record<string, number> = { ms: 1, s: 1000, m: 60_000, h: 3_600_000 };

// Parses "1h30m", "250ms" or a bare number into milliseconds. A bare number is
// seconds, matching the config files this replaces.
export function parseDuration(input: string): number {
  if (/^\d+$/.test(input)) return Number(input) * 1000;
  let total = 0;
  for (const [, n, unit] of input.matchAll(/(\d+)(ms|s|m|h)/g)) total += Number(n) * UNITS[unit]!;
  return total;
}
EOF
  cat > src/duration.test.ts <<'EOF'
import { test, expect } from "bun:test";
import { parseDuration } from "./duration";

test("units", () => {
  expect(parseDuration("1h30m")).toBe(5_400_000);
  expect(parseDuration("250ms")).toBe(250);
});

test("a bare number is seconds", () => {
  expect(parseDuration("90")).toBe(90_000);
});
EOF
  git add -A
  commit "Add parseDuration()"
)

# dirty-tree
(
  scaffold fixtures/dirty-tree pager && paginate
  sed -i.bak 's|^// Pages are 1-based.|// Pages are 1-based (page 0 is an error).|' src/paginate.ts && rm src/paginate.ts.bak
  printf '# notes\n\nTry a cursor-based variant.\n' > NOTES.md
)

# cache-aliasing: HEAD memoizes parseFlags(), which reads as a pure function.
# But effectiveFlags(), from the commit before and untouched by the diff,
# pushes defaults into the array parseFlags returns. Once that array is the
# cached one, defaults leak between callers. The diff is harmless read alone;
# calling the code twice shows the bug.
(
  scaffold fixtures/cache-aliasing flags
  cat > src/flags.ts <<'EOF'
// Parses a comma-separated list of feature flags, e.g. "beta, dark-mode".
export function parseFlags(raw: string): string[] {
  return raw
    .split(",")
    .map((f) => f.trim().toLowerCase())
    .filter(Boolean);
}
EOF
  cat > src/user.ts <<'EOF'
import { parseFlags } from "./flags";

export type User = { id: string; flags: string };

// The flags a user ends up with: their own, plus the plan's defaults that they
// haven't already got.
export function effectiveFlags(user: User, planDefaults: string[]): string[] {
  const flags = parseFlags(user.flags);
  for (const flag of planDefaults) {
    if (!flags.includes(flag)) flags.push(flag);
  }
  return flags;
}

export function hasFlag(user: User, planDefaults: string[], flag: string): boolean {
  return effectiveFlags(user, planDefaults).includes(flag);
}
EOF
  cat > src/flags.test.ts <<'EOF'
import { test, expect } from "bun:test";
import { parseFlags } from "./flags";

test("parses and normalises", () => {
  expect(parseFlags(" Beta, dark-mode ,")).toEqual(["beta", "dark-mode"]);
});
EOF
  cat > src/user.test.ts <<'EOF'
import { test, expect } from "bun:test";
import { effectiveFlags, hasFlag } from "./user";

test("adds plan defaults the user doesn't have", () => {
  expect(effectiveFlags({ id: "u1", flags: "beta" }, ["beta", "export"])).toEqual(["beta", "export"]);
});

test("hasFlag", () => {
  expect(hasFlag({ id: "u2", flags: "dark-mode" }, [], "dark-mode")).toBe(true);
});
EOF
  git add -A
  commit "Add feature flags"
  cat > src/flags.ts <<'EOF'
const cache = new Map<string, string[]>();

// Parses a comma-separated list of feature flags, e.g. "beta, dark-mode".
// Called on every request with a handful of distinct strings, so results are
// cached by the raw string.
export function parseFlags(raw: string): string[] {
  const cached = cache.get(raw);
  if (cached) return cached;
  const flags = raw
    .split(",")
    .map((f) => f.trim().toLowerCase())
    .filter(Boolean);
  cache.set(raw, flags);
  return flags;
}
EOF
  cat >> src/flags.test.ts <<'EOF'

test("repeated input gives the same result", () => {
  expect(parseFlags("alpha")).toEqual(["alpha"]);
  expect(parseFlags("alpha")).toEqual(["alpha"]);
});
EOF
  git add -A
  commit "Cache parseFlags results"
)

# weak-tests: formatBytes() is correct, and its tests look exhaustive (each
# unit, the B/KB edge, the TB cap, bad input). None of them reaches the one
# subtle rule: a value that would show as 1024.0 KB moves up to 1.0 MB. Delete
# that rule and every test still passes. Ground truth is that specific gap,
# not "the tests are thin".
(
  scaffold fixtures/weak-tests bytes
  cat > src/bytes.ts <<'EOF'
const UNITS = ["B", "KB", "MB", "GB", "TB"];

// Formats a byte count for display: 1536 -> "1.5 KB". Bytes are shown as-is,
// larger units with one decimal. A value that would round to "1024.0" of a
// unit moves up to the next one instead, so 1048570 is "1.0 MB", not
// "1024.0 KB". TB is the largest unit.
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) throw new RangeError(`bad byte count: ${bytes}`);
  let unit = 0;
  let value = bytes;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit++;
  }
  if (unit > 0 && value >= 1023.95 && unit < UNITS.length - 1) {
    value /= 1024;
    unit++;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${UNITS[unit]}`;
}
EOF
  cat > src/bytes.test.ts <<'EOF'
import { test, expect } from "bun:test";
import { formatBytes } from "./bytes";

const KB = 1024, MB = KB * 1024, TB = MB * 1024 * 1024;

test("zero", () => expect(formatBytes(0)).toBe("0 B"));
test("largest byte count", () => expect(formatBytes(1023)).toBe("1023 B"));
test("exactly one KB", () => expect(formatBytes(KB)).toBe("1.0 KB"));
test("fractional KB", () => expect(formatBytes(1536)).toBe("1.5 KB"));
test("exactly one MB", () => expect(formatBytes(MB)).toBe("1.0 MB"));
test("TB", () => expect(formatBytes(5 * TB)).toBe("5.0 TB"));
test("TB is the largest unit", () => expect(formatBytes(1024 * TB)).toBe("1024.0 TB"));
test("rejects bad input", () => {
  expect(() => formatBytes(-1)).toThrow(RangeError);
  expect(() => formatBytes(NaN)).toThrow(RangeError);
});
EOF
  git add -A
  commit "Add formatBytes()"
)

for d in fixtures/*/; do
  ( cd "$d" && bun test >/dev/null 2>&1 ) || { echo "tests fail in $d" >&2; exit 1; }
  printf '%-28s HEAD %s, %s uncommitted\n' "$d" "$(git -C "$d" log -1 --format='%h %s')" "$(git -C "$d" status --porcelain | wc -l | tr -d ' ')"
done
