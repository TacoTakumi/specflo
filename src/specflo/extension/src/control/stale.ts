/**
 * Stale-state probes: is a leftover socket alive, is a recorded pid alive.
 *
 * The connect-or-cleanup pattern here - probe the existing socket first and
 * unlink only when nothing answers, so a live listener is never stolen - is
 * lifted from the remote_pi project's supervisor single-instance guard
 * (its `_probeSupervisor` / `_bindUds`).
 *
 * Lifted code attribution: remote_pi, copyright (c) 2026 Jacob Moura,
 * MIT license.
 */

import * as net from "node:net";

/**
 * Whether a live server is accepting connections on ``path``: true when the
 * connect succeeds, false on ECONNREFUSED / ENOENT / ENOTSOCK (a stale file
 * from a crashed session) or a hung listener that answers nothing in time.
 */
export function probeSocket(path: string, timeoutMs = 1000): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const sock = net.createConnection({ path });
    const done = (alive: boolean) => {
      sock.removeAllListeners();
      sock.destroy();
      resolve(alive);
    };
    const timer = setTimeout(() => done(false), timeoutMs);
    sock.once("connect", () => {
      clearTimeout(timer);
      done(true);
    });
    sock.once("error", () => {
      clearTimeout(timer);
      done(false);
    });
  });
}

/** Whether ``pid`` names a live process (signal 0 probe). */
export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (exc) {
    // EPERM: the process exists but is not ours to signal - alive.
    return (exc as NodeJS.ErrnoException).code === "EPERM";
  }
}
