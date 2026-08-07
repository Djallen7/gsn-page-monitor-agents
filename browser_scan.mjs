/**
 * The hourly browser lane: Chromium and WebKit, desktop and mobile, against the
 * REAL WordPress pages - not the Vercel builds on their own.
 *
 *   node browser_scan.mjs            -> data/_browsers.json
 *   node browser_scan.mjs stations   -> just one target, for debugging
 *
 * Why it loads the WordPress page and not the Vercel URL: the Vercel build can be
 * perfect while the page the public actually visits is blank. That gap is the entire
 * reason this exists.
 *
 * Playwright CAN see inside these cross-origin iframes. In-page JavaScript cannot
 * (correctly - that is the browser's same-origin rule doing its job), but Playwright
 * drives the browser from outside, so frame.locator() reaches every link and control
 * inside the embed. Verified against /stations/ before this file was written.
 *
 * A note on `page.$$eval` / `page.evaluate` below: these are Playwright's selector and
 * page-context APIs, not JavaScript's eval(). Every function passed to them is a literal
 * written in this file - nothing from the monitored pages is ever executed as code here.
 *
 * Rows map to the contract's four slots as: Chromium -> "chrome", WebKit -> "safari".
 * WebKit is a STAND-IN. The daily safari_scan.mjs overwrites those rows with real
 * Safari when it runs, and every row's `notes` says which engine produced it.
 */

import { chromium, webkit } from 'playwright';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REGISTRY = JSON.parse(readFileSync(join(HERE, 'targets.json'), 'utf8'));

const VIEWPORTS = {
  desktop: { width: 1280, height: 800 },
  mobile: { width: 390, height: 844 },   // iPhone 15 logical size
};
const ENGINES = [
  { row: 'chrome', label: 'Chromium', launch: chromium },
  { row: 'safari', row_note: 'Playwright WebKit (stand-in for Safari)', label: 'WebKit', launch: webkit },
];

const NAV_TIMEOUT = 45_000;
const SETTLE_MS = 3_000;

const NOISE = REGISTRY.known_noise;
const RULES = REGISTRY.browser_rules;
const FIRST_PARTY = new Set(REGISTRY.link_check.allowed_hosts);

const isKnownRequestNoise = (url) =>
  NOISE.request_substrings.some((n) => url.includes(n.match));
const isKnownConsoleNoise = (text) =>
  NOISE.console_substrings.some((n) => text.includes(n.match));

function isFirstParty(url) {
  try {
    return FIRST_PARTY.has(new URL(url).hostname);
  } catch {
    return false;
  }
}

/**
 * Only first-party http(s) links are ever written to disk.
 *
 * Two reasons, both hard rules. CONTRACT.md: "Never mirror page content into the
 * repo." And these reports are PUBLIC - an unfiltered harvest pulls `mailto:` and
 * `tel:` links straight off the pages and turns the repo into a scraped contact
 * list. The link sweep only checks first-party hosts anyway, so nothing is lost.
 */
function keepableLinks(set) {
  return [...set].filter((h) => /^https?:\/\//i.test(h) && isFirstParty(h));
}

async function scanOne(page, spec, viewportName) {
  const consoleErrors = [];        // everything, for the count
  const consoleSignal = [];        // minus known noise
  const failedAll = [];            // everything, for the count
  const failedSignal = [];         // first-party, minus known noise: these can alarm
  const suppressed = [];           // what we chose not to alarm on, and why
  const mixedContent = [];
  const problems = [];
  const links = new Set();

  const noteFailure = (label, url) => {
    failedAll.push(label);
    if (isKnownRequestNoise(url)) { suppressed.push(label); return; }
    if (RULES.first_party_only && !isFirstParty(url)) { suppressed.push(label); return; }
    failedSignal.push(label);
  };

  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    consoleErrors.push(m.text());
    if (!isKnownConsoleNoise(m.text())) consoleSignal.push(m.text());
  });
  page.on('pageerror', (e) => {
    consoleErrors.push(`pageerror: ${e.message}`);
    consoleSignal.push(`pageerror: ${e.message}`);
  });
  page.on('requestfailed', (r) => {
    const f = r.failure();
    if (['document', 'script', 'stylesheet', 'xhr', 'fetch'].includes(r.resourceType())) {
      noteFailure(`${r.resourceType()} ${r.url()} (${f ? f.errorText : 'failed'})`, r.url());
    }
  });
  page.on('response', (r) => {
    if (r.status() >= 400) noteFailure(`${r.status()} ${r.url()}`, r.url());
  });
  page.on('request', (r) => {
    if (r.url().startsWith('http://')) mixedContent.push(r.url());
  });

  const entry = (spec.wordpress || spec.vercel).url;
  let status = null;
  try {
    const resp = await page.goto(entry, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    status = resp ? resp.status() : null;
  } catch (e) {
    return {
      browser: null, viewport: viewportName, ok: false,
      console_errors: consoleErrors.length, failed_requests: failedAll.length,
      notes: `navigation failed: ${e.message.split('\n')[0]}`, links: [],
    };
  }
  await page.waitForTimeout(SETTLE_MS);

  const bcfg = spec.browser || {};

  // --- the crew-only app: the ONLY correct outcome is still being refused ------
  if (bcfg.expect_auth_wall) {
    const body = (await page.textContent('body').catch(() => '')) || '';
    const refused = status === 401 || /authenticat|log in to vercel|access denied/i.test(body);
    if (!refused) {
      problems.push(`AUTH WALL GONE: expected to be refused, got ${status}. A crew tool may be public.`);
    }
    return {
      browser: null, viewport: viewportName, ok: problems.length === 0,
      console_errors: consoleErrors.length, failed_requests: 0,
      notes: problems.join(' | ') || `still refusing anonymous visitors (${status})`,
      links: [],
    };
  }

  if (status !== (spec.wordpress || spec.vercel).expected_status) {
    problems.push(`page returned ${status}, expected ${(spec.wordpress || spec.vercel).expected_status}`);
  }

  // --- links on the wrapper page itself ---------------------------------------
  for (const h of await page.$$eval('a[href]', (as) => as.map((a) => a.href)).catch(() => [])) {
    links.add(h);
  }

  // --- horizontal overflow: the classic mobile break ---------------------------
  const overflow = await page.evaluate(() => {
    const d = document.documentElement;
    return { scroll: d.scrollWidth, inner: window.innerWidth };
  });
  if (overflow.scroll > overflow.inner + 2) {
    problems.push(`horizontal overflow: content is ${overflow.scroll}px wide in a ${overflow.inner}px window`);
  }

  // --- the embed ---------------------------------------------------------------
  if (bcfg.check_embed) {
    const expected = spec.wordpress?.expected_iframe_src;
    const frames = page.frames().filter((f) => f !== page.mainFrame());
    const iframeEl = await page.$('iframe');

    if (!iframeEl) {
      problems.push('no <iframe> on the page at all - the embed tag is gone');
    } else {
      const src = await iframeEl.getAttribute('src');
      if (expected && src && src.replace(/\/$/, '') !== expected.replace(/\/$/, '')) {
        problems.push(`embed points at ${src} but should point at ${expected}`);
      }
      const box = await iframeEl.boundingBox();
      const minH = bcfg.min_iframe_height ?? 300;
      if (!box) {
        problems.push('embed is present but not rendered (no box) - it is collapsed or hidden');
      } else if (box.height < minH) {
        problems.push(`embed rendered only ${Math.round(box.height)}px tall, under the ${minH}px floor - collapsed`);
      }

      // Reach INSIDE the embed. This is the check that proves the page a visitor
      // sees is actually populated, not a blank box in a correct-looking wrapper.
      const target = frames.find((f) => expected ? f.url().startsWith(expected.replace(/\/$/, '')) : true);
      if (!target) {
        problems.push('the embed never loaded a document - blocked, or the build did not respond');
      } else {
        const inner = await target.$$eval('a[href]', (as) => as.map((a) => a.href)).catch(() => []);
        inner.forEach((h) => links.add(h));
        const minLinks = bcfg.min_frame_links ?? 0;
        if (inner.length < minLinks) {
          problems.push(`only ${inner.length} links inside the embed, expected at least ${minLinks} - the build loaded but rendered nothing useful`);
        }
        const interactive = await target
          .$$eval('a[href],button,[role="button"],input,select', (els) => els.length)
          .catch(() => 0);
        if (minLinks > 0 && interactive === 0) {
          problems.push('no interactive elements inside the embed');
        }
        // Some embeds carry almost no links but plenty of controls (the homepage's
        // video player has 24 controls and 0 links), so they are floored on controls
        // instead. Both floors sit well under what was measured, to avoid flapping.
        const minControls = bcfg.min_frame_controls ?? 0;
        if (interactive < minControls) {
          problems.push(`only ${interactive} controls inside the embed, expected at least ${minControls} - the build loaded but rendered nothing useful`);
        }
        // Broken images INSIDE the embed.
        //
        // This check exists because the first version of this monitor missed a real
        // outage: on 2026-08-06 the stations page rendered 19 of its 25 images blank
        // for every visitor, while every other check here passed. Nothing caught it,
        // because the images never 404 - they are never REQUESTED at all. The parent
        // pins the iframe to a fixed height and it never scrolls, so `loading="lazy"`
        // images below the frame's viewport never intersect it. Standalone the build
        // is flawless; only the embedded context breaks. Status codes cannot see this
        // class of fault - only counting what actually rendered can.
        const imgs = await target
          .evaluate(() => {
            const all = [...document.images];
            const bad = all.filter((i) => !i.complete || i.naturalWidth === 0);
            return {
              total: all.length,
              broken: bad.length,
              first: bad.slice(0, 3).map((i) => (i.getAttribute('src') || '').split('/').pop()),
            };
          })
          .catch(() => null);
        const maxBroken = bcfg.max_broken_images ?? 0;
        if (imgs && imgs.broken > maxBroken) {
          problems.push(`${imgs.broken} of ${imgs.total} images did not render inside the embed (${imgs.first.join(', ')}) - visitors see blank spaces`);
        }

        // The strongest in-frame assertion available: a string that only exists if the
        // build actually rendered its own content, not an error page or empty shell.
        // textContent, NOT innerText. innerText is the *rendered* text: these headings
        // are split across elements and CSS-uppercased, so the literal string never
        // appears in innerText even when the page is perfect. textContent also matches
        // the semantics of the HTTP-layer proof strings, which search raw markup.
        if (bcfg.frame_proof_string) {
          const text = await target
            .evaluate(() => (document.body ? document.body.textContent : ''))
            .catch(() => '');
          if (!text.includes(bcfg.frame_proof_string)) {
            problems.push(`"${bcfg.frame_proof_string}" is missing from inside the embed - the frame loaded but the content is not there`);
          }
        }
      }
    }
  }

  // --- observations that DO alarm ---------------------------------------------
  if (mixedContent.length) {
    problems.push(`${mixedContent.length} insecure http:// request(s), e.g. ${mixedContent[0]}`);
  }
  if (failedSignal.length) {
    problems.push(`${failedSignal.length} failed request(s) on our own hosts: ${failedSignal[0].slice(0, 160)}`);
  }
  const ceiling = RULES.max_console_errors[spec.id];
  if (typeof ceiling === 'number' && consoleErrors.length > ceiling) {
    problems.push(`${consoleErrors.length} console errors, over the ${ceiling} baseline - something new is throwing: ${(consoleSignal[0] || consoleErrors[0] || '').slice(0, 160)}`);
  }

  // --- observations that are only recorded ------------------------------------
  const observed = [];
  if (consoleErrors.length) observed.push(`${consoleErrors.length} console errors (baseline ${ceiling ?? 'n/a'})`);
  if (suppressed.length) observed.push(`${suppressed.length} known-noise requests suppressed`);

  return {
    browser: null,
    viewport: viewportName,
    ok: problems.length === 0,
    console_errors: consoleErrors.length,
    failed_requests: failedAll.length,
    notes: problems.length ? problems.join(' | ') : observed.join('; '),
    links: keepableLinks(links),
  };
}

async function main() {
  const only = process.argv[2];
  const targets = REGISTRY.targets
    .filter((t) => !only || t.id === only)
    .sort((a, b) => a.order - b.order);

  const out = { generated_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), targets: {} };
  for (const t of targets) out.targets[t.id] = { browsers: [] };

  for (const engine of ENGINES) {
    let browser;
    try {
      browser = await engine.launch.launch();
    } catch (e) {
      console.error(`${engine.label} failed to launch: ${e.message}`);
      for (const t of targets) {
        for (const vp of Object.keys(VIEWPORTS)) {
          out.targets[t.id].browsers.push({
            browser: engine.row, viewport: vp, ok: null, console_errors: null,
            failed_requests: null, notes: `not run - ${engine.label} would not launch`, links: [],
          });
        }
      }
      continue;
    }

    for (const [vpName, vp] of Object.entries(VIEWPORTS)) {
      const context = await browser.newContext({
        viewport: vp,
        ignoreHTTPSErrors: false,
        userAgent: undefined,
      });
      for (const t of targets) {
        const page = await context.newPage();
        let row;
        try {
          row = await scanOne(page, t, vpName);
        } catch (e) {
          row = {
            browser: null, viewport: vpName, ok: false, console_errors: null,
            failed_requests: null, notes: `scan crashed: ${e.message.split('\n')[0]}`, links: [],
          };
        }
        await page.close().catch(() => {});
        row.browser = engine.row;
        row.notes = [engine.row_note || engine.label, row.notes].filter(Boolean).join(' - ');
        out.targets[t.id].browsers.push(row);
        console.log(`${row.ok === true ? 'ok  ' : row.ok === false ? 'FAIL' : '... '} ${engine.label}/${vpName} ${t.id}${row.ok === false ? ' :: ' + row.notes : ''}`);
      }
      await context.close();
    }
    await browser.close();
  }

  mkdirSync(join(HERE, 'data'), { recursive: true });
  writeFileSync(join(HERE, 'data', '_browsers.json'), JSON.stringify(out, null, 2) + '\n');
  console.log(`\nwrote data/_browsers.json`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
