/**
 * The deny-list extension against a fake pi: what it blocks, what it lets
 * through, and what the blocked member is told.
 *
 * The fake carries only what the extension touches: `on` records handlers and
 * `call` runs the tool_call handlers the way pi does, returning the first
 * blocking result. No pi process and no model.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import register, { DENY_ENV, denyRules, matchRule } from "./deny.ts";

type Handler = (event: any, ctx: any) => unknown;

function load(env: Record<string, string | undefined>) {
  const handlers = new Map<string, Handler[]>();
  const api = {
    on(event: string, handler: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), handler]);
    },
  };
  register(api as any, env);
  return {
    handlers,
    async call(toolName: string, input: Record<string, unknown>): Promise<any> {
      for (const handler of handlers.get("tool_call") ?? []) {
        const result: any = await handler({ type: "tool_call", toolCallId: "call-1", toolName, input }, {});
        if (result?.block) return result;
      }
      return undefined;
    },
  };
}

const bash = (command: string) => ({ command });

describe("a member whose definition denies 'git push'", () => {
  const env = { [DENY_ENV]: JSON.stringify(["git push"]) };

  it("has 'git push origin main' blocked with an error naming the rule", async () => {
    const result = await load(env).call("bash", bash("git push origin main"));

    assert.equal(result.block, true);
    assert.match(result.reason, /'git push'/);
    assert.match(result.reason, /deny list/);
  });

  it("runs 'git status'", async () => {
    assert.equal(await load(env).call("bash", bash("git status")), undefined);
  });

  it("is blocked when the push is one part of a longer command line", async () => {
    const pi = load(env);

    for (const command of ["cd repo && git push", "(git push)", "true;git push -f", "git   push"]) {
      assert.equal((await pi.call("bash", bash(command)))?.block, true, command);
    }
  });

  it("is not blocked by a word that only starts or ends like the rule", async () => {
    const pi = load(env);

    for (const command of ["git pushd-notes", "legit push", "git log --grep push"]) {
      assert.equal(await pi.call("bash", bash(command)), undefined, command);
    }
  });

  it("has only its bash calls checked", async () => {
    const result = await load(env).call("write", { path: "notes.md", content: "then git push" });

    assert.equal(result, undefined);
  });
});

describe("the rule an error names", () => {
  it("is the first rule on the list that matches", () => {
    assert.equal(matchRule("rm -rf build && git push", ["git push", "rm -rf"]), "git push");
    assert.equal(matchRule("rm -rf build", ["git push", "rm -rf"]), "rm -rf");
    assert.equal(matchRule("ls", ["git push", "rm -rf"]), undefined);
  });

  it("is matched as written, never as a regular expression", () => {
    assert.equal(matchRule("cat a.b", ["a.b"]), "a.b");
    assert.equal(matchRule("cat axb", ["a.b"]), undefined);
  });
});

describe("a member whose definition denies nothing", () => {
  it("has no variable set and nothing blocked", async () => {
    assert.deepEqual(denyRules({}), []);
    assert.equal(await load({}).call("bash", bash("git push origin main")), undefined);
  });
});

describe("a deny list that cannot be read", () => {
  it("blocks every bash call and names the variable, rather than letting all through", async () => {
    for (const value of ["not json", JSON.stringify("git push"), JSON.stringify(["git push", 7])]) {
      const result = await load({ [DENY_ENV]: value }).call("bash", bash("git status"));

      assert.equal(result?.block, true, value);
      assert.match(result.reason, new RegExp(DENY_ENV), value);
    }
  });
});
