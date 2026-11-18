/** Run explicitly with node dist/enroll-installation.js; never on import. */
import { readSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { assertEnrollmentPlatform, enrollInstallation, EnrollmentError } from "./install-authority.js";

export async function runEnrollmentCommand(args: string[]): Promise<void> {
  assertEnrollmentPlatform();
  const bad = (): never => { throw new EnrollmentError("enrollment_input_invalid"); };
  const [mode, ...flags] = args;
  if (mode !== "register" && mode !== "handoff" && mode !== "resume") bad();
  const options = new Map<string, string>();
  for (let i = 0; i < flags.length; i += 2) {
    const key = flags[i], value = flags[i + 1];
    if (!["--gateway", "--expect-tenant", "--expect-fleet", "--credential-fd"].includes(key) ||
        !value || options.has(key) || value.startsWith("--")) bad();
    options.set(key, value);
  }
  const gateway = options.get("--gateway"), expectedTenant = options.get("--expect-tenant");
  if (!gateway || !expectedTenant) bad();
  const descriptor = options.get("--credential-fd");
  if (mode !== "resume" && descriptor === undefined) bad();
  let input: Record<string, unknown> = {};
  if (descriptor !== undefined) {
    if (!/^(0|[3-9]|[1-9][0-9]+)$/.test(descriptor) || !Number.isSafeInteger(Number(descriptor))) bad();
    try {
      const chunks: Buffer[] = []; let size = 0;
      let deadline: number | undefined;
      while (true) {
        if (deadline !== undefined && performance.now() >= deadline) bad();
        const chunk = Buffer.alloc(1024); let n: number;
        try { n = readSync(Number(descriptor), chunk, 0, chunk.length, null); }
        catch (e) {
          const code = (e as NodeJS.ErrnoException).code;
          if (code === "EOF") break;
          if (code !== "EAGAIN" && code !== "EWOULDBLOCK") bad();
          deadline ??= performance.now() + 5000;
          await delay(25); continue;
        }
        if (!n) break;
        size += n; if (size > 8192) bad(); chunks.push(chunk.subarray(0, n));
      }
      input = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(Buffer.concat(chunks)));
      if (!input || typeof input !== "object" || Array.isArray(input)) bad();
    } catch { bad(); }
  }
  await enrollInstallation({mode: mode as "register" | "handoff" | "resume", gateway: gateway!,
    expectedTenant: expectedTenant!, expectedFleet: options.get("--expect-fleet"), input});
  console.log("enrollment_verified");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runEnrollmentCommand(process.argv.slice(2)).catch((e: unknown) => {
    console.error(e instanceof EnrollmentError ? e.message : "enrollment_state_invalid");
    process.exitCode = 1;
  });
}
