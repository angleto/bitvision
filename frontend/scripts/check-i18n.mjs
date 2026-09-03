#!/usr/bin/env node
/**
 * Every statically written translation key must resolve, in every locale.
 *
 * A missing key is not a build error and not a type error: `next-intl`
 * renders the key path itself, so the page ships and the user reads
 * `patient.contactDelete.action` where a button label should be. On a
 * screen that asks somebody to confirm removing a person's access to a
 * health record, that is not a cosmetic failure.
 *
 * How it decides what a key is
 * ---------------------------
 *
 * A file may declare several namespaces (`const t = useTranslations("patient")`,
 * `const tShare = useTranslations("share")`, and often a second `t` inside a
 * nested component). Binding each call to the right one needs real scope
 * analysis; instead this collects every namespace declared in the file and
 * accepts a key that resolves under *any* of them.
 *
 * That is deliberately loose in one direction and exact in the other: a key
 * written under the wrong namespace still passes, but a key that exists
 * nowhere — a typo, a rename, a locale someone forgot — fails. The tight
 * version produced dozens of false positives on this codebase, and a check
 * people learn to ignore is worse than no check.
 *
 * Only literal keys are checked. `t(\`x.${kind}\`)` is invisible here by
 * construction and stays the author's responsibility.
 *
 * Also asserts that `en` and `it` carry exactly the same key set, which is
 * what actually goes wrong: a key added to one locale and forgotten in the
 * other renders as the key path for half the users.
 *
 *   node scripts/check-i18n.mjs
 */

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const LOCALES = ["en", "it"];

/**
 * Duplicate object keys, reported with their full path.
 *
 * `JSON.parse` keeps the LAST of two duplicate keys and silently discards
 * whatever the first held, and a reviver cannot see it either — by the time
 * the reviver runs, the overwrite has already happened. So the raw text is
 * scanned: a small state machine that tracks the nesting path and records
 * each key as it appears lexically.
 */
function findDuplicateKeys(raw) {
  const dupes = [];
  const stack = []; // path segments of the enclosing objects
  const seen = []; // one Set per open object
  let i = 0;
  let pendingKey = null; // last string read, until ':' proves it a key
  const inArray = []; // whether each open container is an array

  const readString = () => {
    let out = "";
    i++; // opening quote
    while (i < raw.length) {
      const ch = raw[i];
      if (ch === "\\") {
        out += raw[i + 1];
        i += 2;
        continue;
      }
      if (ch === '"') {
        i++;
        return out;
      }
      out += ch;
      i++;
    }
    return out;
  };

  while (i < raw.length) {
    const ch = raw[i];
    if (ch === '"') {
      pendingKey = readString();
      continue;
    }
    if (ch === "{") {
      seen.push(new Set());
      inArray.push(false);
      if (pendingKey !== null) {
        stack.push(pendingKey);
        pendingKey = null;
      } else stack.push(null);
      i++;
      continue;
    }
    if (ch === "[") {
      inArray.push(true);
      i++;
      continue;
    }
    if (ch === "]") {
      inArray.pop();
      i++;
      continue;
    }
    if (ch === "}") {
      seen.pop();
      inArray.pop();
      stack.pop();
      pendingKey = null;
      i++;
      continue;
    }
    if (ch === ":") {
      if (pendingKey !== null && seen.length > 0) {
        const here = seen[seen.length - 1];
        if (here.has(pendingKey)) {
          const trail = [...stack.filter(Boolean), pendingKey].join(".");
          dupes.push(trail);
        }
        here.add(pendingKey);
      }
      pendingKey = null;
      i++;
      continue;
    }
    if (ch === ",") {
      pendingKey = null;
      i++;
      continue;
    }
    i++;
  }
  return dupes;
}

function loadLocale(locale) {
  const file = path.join(ROOT, "messages", `${locale}.json`);
  const raw = readFileSync(file, "utf8");
  return { data: JSON.parse(raw), dupes: findDuplicateKeys(raw), file };
}

function flatten(obj, prefix = "") {
  const out = [];
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) out.push(...flatten(v, key));
    else out.push(key);
  }
  return out;
}

function resolve(obj, dotted) {
  return dotted.split(".").reduce((acc, part) => (acc == null ? acc : acc[part]), obj);
}

function sourceFiles(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(p, out);
    else if (/\.(tsx|ts)$/.test(entry.name)) out.push(p);
  }
  return out;
}

const NS_RE = /useTranslations\(\s*["'`]([^"'`]+)["'`]\s*\)/g;
// A call on any identifier, with a literal first argument. The leading
// character class stops `.get("token")` from reading as `t("token")`.
const CALL_RE = /(?:^|[^\w.$])([A-Za-z_$][\w$]*)\(\s*["']([A-Za-z0-9_][\w.]*)["']/g;

const failures = [];
const locales = {};
for (const locale of LOCALES) {
  const loaded = loadLocale(locale);
  for (const d of loaded.dupes) {
    failures.push(`${loaded.file}: duplicate key ${d} — JSON.parse silently keeps the last one`);
  }
  locales[locale] = loaded.data;
}

// --- locale parity ---------------------------------------------------
const keySets = Object.fromEntries(LOCALES.map((l) => [l, new Set(flatten(locales[l]))]));
const [base, ...rest] = LOCALES;
for (const other of rest) {
  for (const k of keySets[base]) {
    if (!keySets[other].has(k)) failures.push(`missing in ${other}: ${k}`);
  }
  for (const k of keySets[other]) {
    if (!keySets[base].has(k)) failures.push(`missing in ${base}: ${k}`);
  }
}

// --- static key resolution -------------------------------------------
let checked = 0;
/**
 * Blank out comments and string literals that are not the argument we are
 * looking for. Without this, a usage example inside a doc comment reads as
 * a real call site: `ModalHost.tsx` documents `t("areYouSure")` in its
 * header and the check reported a key that no code ever asks for.
 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/(^|[^:])\/\/[^\n]*/g, (m, p1) => p1 + " ".repeat(m.length - p1.length));
}

for (const file of sourceFiles(path.join(ROOT, "src"))) {
  const src = stripComments(readFileSync(file, "utf8"));

  const namespaces = [...src.matchAll(NS_RE)].map((m) => m[1]);
  if (namespaces.length === 0) continue;
  // Names bound to a namespace in this file, plus the conventional ones,
  // so an unrelated `foo("bar.baz")` is not mistaken for a lookup.
  const translatorNames = new Set(
    [
      ...src.matchAll(
        /(?:const|let)\s+([\w$]+)\s*=\s*(?:await\s+)?(?:useTranslations|getTranslations)\(/g,
      ),
    ].map((m) => m[1]),
  );
  if (translatorNames.size === 0) continue;

  for (const m of src.matchAll(CALL_RE)) {
    const [, callee, key] = m;
    if (!translatorNames.has(callee)) continue;
    checked++;
    const resolvesSomewhere = namespaces.some((ns) =>
      LOCALES.every((l) => resolve(locales[l], `${ns}.${key}`) !== undefined),
    );
    if (!resolvesSomewhere) {
      const line = src.slice(0, m.index).split("\n").length;
      failures.push(
        `${path.relative(ROOT, file)}:${line}: "${key}" resolves under none of ` +
          `[${namespaces.join(", ")}] in all locales`,
      );
    }
  }
}

if (failures.length > 0) {
  console.error(`i18n check FAILED — ${failures.length} problem(s):\n`);
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}
console.error(
  `i18n check OK — ${checked} static keys across ${LOCALES.length} locales, ` +
    `${keySets[base].size} keys per locale, no drift.`,
);
