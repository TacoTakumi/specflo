/**
 * Session identity for the control surface: which name the per-session state
 * directory is keyed by, and whether the session is managed or adopted.
 *
 * A managed start hands the extension an env handshake: SPECFLO_AGENT_NAME is
 * used verbatim (the CLI owns its validity and uniqueness) and
 * SPECFLO_AGENT_MANAGED marks ownership. Without a handshake the session was
 * started by hand and is adoptable: the name derives from the pi session name
 * when one is set, else the cwd basename, sanitized to the v1 agent-name
 * alphabet, with a pid suffix when the candidate is already claimed by
 * another session's record.
 */

export const ENV_AGENT_NAME = "SPECFLO_AGENT_NAME";
export const ENV_AGENT_MANAGED = "SPECFLO_AGENT_MANAGED";

export type Ownership = "managed" | "adopted";

export interface Identity {
  name: string;
  ownership: Ownership;
}

export interface IdentityInput {
  /** Environment to read the handshake from (process.env in production). */
  env: Record<string, string | undefined>;
  /** The pi session's display name, when one is set. */
  sessionName?: string | undefined;
  /** The session working directory. */
  cwd: string;
  /** This process's pid, the collision suffix. */
  pid: number;
  /** Whether a name is already claimed by an existing record. */
  claimed: (name: string) => boolean;
}

/**
 * A derived name squeezed into v1's agent-name alphabet
 * (``^[A-Za-z0-9][A-Za-z0-9._-]*$``): runs of invalid characters become one
 * '-', an invalid leading run is dropped, and an empty result falls back to
 * "pi" so the layout always gets a keyable name.
 */
function sanitize(raw: string): string {
  const cleaned = raw.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");
  return cleaned === "" ? "pi" : cleaned;
}

export function deriveIdentity(input: IdentityInput): Identity {
  const handshake = input.env[ENV_AGENT_NAME];
  if (handshake) {
    const managed = input.env[ENV_AGENT_MANAGED];
    return {
      name: handshake,
      ownership: managed === "1" || managed === "true" ? "managed" : "adopted",
    };
  }
  const candidate = sanitize(
    input.sessionName ? input.sessionName : basename(input.cwd),
  );
  return {
    name: input.claimed(candidate) ? `${candidate}-${input.pid}` : candidate,
    ownership: "adopted",
  };
}

/** Last path component, both separators, with no node:path import needed. */
function basename(p: string): string {
  const parts = p.split(/[\\/]+/).filter((part) => part !== "");
  return parts.length > 0 ? parts[parts.length - 1] : "pi";
}
