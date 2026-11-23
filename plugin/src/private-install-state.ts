/** Shared POSIX-only private state primitives; no import-time I/O. */
import {
  closeSync, constants, existsSync, fstatSync, fsyncSync, lstatSync,
  mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync,
} from "node:fs";
import {randomUUID} from "node:crypto";
import {homedir} from "node:os";
import {dirname} from "node:path";
import {getOpenClawBaseDir, getPluginDir} from "./paths.js";

export function privateInstallState(getPath: () => string, fail: (code: string) => never,
  knownError: (e: unknown) => boolean) {
  // Refuse unsafe existing components; do not chmod/reset another installation.
  function privateDirectory(create: boolean): void {
    for (const p of [homedir(), getOpenClawBaseDir(), dirname(getPluginDir()), getPluginDir()]) {
      if (!existsSync(p)) {
        if (!create) return;
        try { mkdirSync(p, {mode: 0o700}); } catch { fail("state_unwritable"); }
      }
      const s = lstatSync(p);
      if (!s.isDirectory() || s.isSymbolicLink() || (s.mode & 0o022) ||
          (process.getuid && s.uid !== process.getuid())) fail("state_unsafe");
    }
  }

  function read(): unknown {
    privateDirectory(false);
    const path = getPath();
    let fd: number;
    try {
      const s = lstatSync(path);
      if (!s.isFile() || s.isSymbolicLink() || s.nlink !== 1 || (s.mode & 0o777) !== 0o600 ||
          s.size > 65536 || (process.getuid && s.uid !== process.getuid())) fail("state_unsafe");
      fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
      if (fstatSync(fd).ino !== s.ino) { closeSync(fd); fail("state_unsafe"); }
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      if (knownError(e)) throw e;
      return fail("state_unsafe");
    }
    let j: unknown;
    try { j = JSON.parse(readFileSync(fd, "utf8")); }
    catch { return fail("state_invalid"); }
    finally { closeSync(fd); }
    return j;
  }

  function syncDirectory(path: string): void {
    const fd = openSync(dirname(path), constants.O_RDONLY);
    try { fsyncSync(fd); } finally { closeSync(fd); }
  }

  function save(j: unknown, replaced?: () => void): void {
    const path = getPath();
    const tmp = `${path}.${randomUUID()}.tmp`;
    let fd: number | undefined;
    try {
      fd = openSync(tmp, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
      writeFileSync(fd, JSON.stringify(j) + "\n"); fsyncSync(fd); closeSync(fd); fd = undefined;
      renameSync(tmp, path); replaced?.(); syncDirectory(path);
    } catch { fail("state_unwritable"); }
    finally {
      if (fd !== undefined) closeSync(fd);
      rmSync(tmp, {force: true});
    }
  }

  function withLock<T>(action: () => Promise<T>, retainOnError: () => boolean = () => false): Promise<T> {
    privateDirectory(true);
    const path = `${getPath()}.lock`;
    let fd: number;
    try { fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600); }
    catch (e) { return fail((e as NodeJS.ErrnoException).code === "EEXIST" ? "locked" : "state_unwritable"); }
    const inode = fstatSync(fd).ino;
    let failed = false;
    return action().catch(e => { failed = true; throw e; }).finally(() => {
      closeSync(fd);
      // A hard crash intentionally leaves a lock for explicit operator recovery.
      if (!(failed && retainOnError()) && lstatSync(path).ino === inode) { rmSync(path); syncDirectory(path); }
    });
  }

  return {read, save, withLock};
}
