/**
 * specflo pool deny list - blocks a pooled member's bash call that matches a
 * rule of its agent definition.
 *
 * This is a guard against mistakes, not a security boundary. It reads the
 * command text and nothing more: a rule is found only where its words stand
 * together as written, so 'git push' stops `git push origin main` and does
 * not stop `git -C repo push`, an alias, a script that pushes, or any other
 * tool. pi has no sandbox. The hard limit on a member is what the pool gives
 * it: a member that must never push is started with no push credential.
 *
 * The pool loads this file with `-e` on every member and hands it the
 * definition's deny list as a JSON array of strings in SPECFLO_POOL_DENY. The
 * list is read once, at load, so nothing the member runs can change it. With
 * the variable unset the member's definition denies nothing. With a value that
 * is not such an array every bash call is blocked: a list that cannot be read
 * must not read as an empty one.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** The variable the pool's launch builder puts the definition's deny list in. */
export const DENY_ENV = "SPECFLO_POOL_DENY";

/**
 * The rules in ``env``, blank ones dropped; none when the variable is unset.
 *
 * Throws for a value that is not a JSON array of strings.
 */
export function denyRules(env: Record<string, string | undefined>): string[] {
  const raw = env[DENY_ENV];
  if (raw === undefined || raw === "") return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed) || !parsed.every((rule) => typeof rule === "string")) {
    throw new Error("not a JSON array of strings");
  }
  return parsed.filter((rule) => rule.trim() !== "");
}

/**
 * The first of ``rules`` that ``command`` matches, if any.
 *
 * A rule matches where its words follow one another in the command, any
 * whitespace between them, as whole words: 'git push' is not found in
 * `legit push` or `git pushd`. A rule is plain text, never a pattern.
 */
export function matchRule(command: string, rules: string[]): string | undefined {
  return rules.find((rule) => {
    const words = rule.trim().split(/\s+/).map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return new RegExp(`(?<![\\w-])${words.join("\\s+")}(?![\\w-])`).test(command);
  });
}

export default function (pi: ExtensionAPI, env: Record<string, string | undefined> = process.env) {
  let rules: string[] = [];
  let unreadable: string | undefined;
  try {
    rules = denyRules(env);
  } catch (error) {
    unreadable = error instanceof Error ? error.message : String(error);
  }

  pi.on("tool_call", (event) => {
    if (event.toolName !== "bash") return undefined;
    if (unreadable !== undefined) {
      return {
        block: true,
        reason: `Blocked: this agent's deny list in ${DENY_ENV} cannot be read (${unreadable}), so no bash command is run.`,
      };
    }
    const rule = matchRule(String((event.input as { command?: unknown }).command ?? ""), rules);
    if (rule === undefined) return undefined;
    return {
      block: true,
      reason: `Blocked by the deny list of this agent's definition: the command matches the rule '${rule}'. Do not try another way to run it; report that it was blocked.`,
    };
  });
}
