import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { getDisplayName, sanitizeHostname } from "./identity.js";

describe("sanitizeHostname", () => {
  test("strips trailing .local", () => {
    assert.equal(sanitizeHostname("MacBook-Pro.local"), "macbook-pro");
  });

  test("strips trailing .lan", () => {
    assert.equal(sanitizeHostname("homebox.lan"), "homebox");
  });

  test("lowercases and replaces underscores with dashes", () => {
    assert.equal(sanitizeHostname("ip_10_0_1_23"), "ip-10-0-1-23");
  });

  test("collapses whitespace and runs of dashes", () => {
    assert.equal(sanitizeHostname("My  Laptop"), "my-laptop");
  });

  test("takes only the first label of cloud-VM FQDNs", () => {
    // GCP-style FQDN — the user-meaningful name is the first label.
    assert.equal(
      sanitizeHostname(
        "erni-openclaw.us-central1-c.c.alpine-theory-469016-c8.internal",
      ),
      "erni-openclaw",
    );
  });

  test("collapses two-label hostnames to the first label too", () => {
    // Pre-fix this kept dots; we now treat all dotted forms uniformly
    // so display names stay short across cloud + on-prem alike.
    assert.equal(sanitizeHostname("prod-cluster-01.useast"), "prod-cluster-01");
  });

  test("drops disallowed characters but preserves dashes/digits", () => {
    assert.equal(sanitizeHostname("foo!bar@baz#1"), "foobarbaz1");
  });

  test("empty input → empty output", () => {
    assert.equal(sanitizeHostname(""), "");
  });
});

describe("getDisplayName", () => {
  test("prefixes baseName with sanitized hostname", () => {
    assert.equal(
      getDisplayName("main", "MacBook-Pro.local"),
      "macbook-pro-main",
    );
  });

  test("strips FQDN tail (cloud-VM hostname → first label)", () => {
    assert.equal(
      getDisplayName(
        "main",
        "erni-openclaw.us-central1-c.c.alpine-theory-469016-c8.internal",
      ),
      "erni-openclaw-main",
    );
  });

  test("falls back to baseName when hostname is empty", () => {
    assert.equal(getDisplayName("main", ""), "main");
  });

  test("uses real OS hostname when no override is given", () => {
    // Doesn't assert exact value (CI hostnames vary) — just that the
    // returned label ends with the baseName and is non-empty.
    const out = getDisplayName("main");
    assert.ok(out.endsWith("main"), `expected ends-with 'main', got ${out}`);
    assert.ok(out.length >= "main".length);
  });

  test("MEMCLAW_DISPLAY_NAME_OVERRIDE wins verbatim", () => {
    // Operator-supplied label takes precedence over hostname-derived
    // default. The trim is intentional — env-var values often have
    // accidental whitespace; an empty / whitespace-only override
    // falls through to hostname.
    assert.equal(
      getDisplayName("main", "host", "Erni's MBP"),
      "Erni's MBP",
    );
    assert.equal(
      getDisplayName("main", "host", "  literally-this  "),
      "literally-this",
    );
  });

  test("blank override falls through to hostname-derived label", () => {
    assert.equal(getDisplayName("main", "host", "   "), "host-main");
    assert.equal(getDisplayName("main", "host", ""), "host-main");
  });
});
