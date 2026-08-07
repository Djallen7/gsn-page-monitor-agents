/**
 * The DAILY lane: real Safari, via safaridriver, on a macOS runner.
 *
 *   node safari_scan.mjs     -> data/_safari.json
 *
 * Deliberately a SMOKE TEST, not the full sweep. It answers one question the hourly
 * lane cannot: does the page work in the browser Daniel and his station GMs actually
 * use? Playwright's WebKit is close but not the same engine build, and the difference
 * bites exactly here - real Safari partitions localStorage and blocks third-party
 * cookies inside cross-origin iframes, which is the precise arrangement every GSN page
 * uses, and the stations build already relies on localStorage. So a WebKit pass is
 * genuinely not evidence that Safari passes.
 *
 * Its output overwrites the "safari" rows in the merge step when it is fresher than
 * 26 hours. If it never runs, those rows stay labelled as WebKit stand-ins. It never
 * fabricates a Safari result.
 *
 * NOT YET VERIFIED ON A GITHUB MACOS RUNNER. `sudo safaridriver --enable` is the
 * documented way to turn the driver on there, but nobody has run it for this repo
 * yet, so the workflow job is off by default and this file is honest about that.
 * If safaridriver refuses to start, every row comes back ok:null with the reason -
 * which renders as "not checked", not as a pass.
 */

import { Builder, By } from 'selenium-webdriver';
import safari from 'selenium-webdriver/safari.js';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REGISTRY = JSON.parse(readFileSync(join(HERE, 'targets.json'), 'utf8'));

// Safari will not shrink below roughly 400px wide, so "mobile" here is 400px, not the
// 390px the hourly lane uses. Recorded rather than hidden: a 10px difference does not
// change whether a layout collapses, but it does change what you compare against.
const VIEWPORTS = { desktop: [1280, 800], mobile: [400, 844] };
const SETTLE_MS = 4000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Only first-party http(s) links are ever written to disk - these reports are public,
// and an unfiltered harvest would scrape mailto:/tel: links off the pages into them.
// Same rule as browser_scan.mjs; CONTRACT.md forbids mirroring page content.
const FIRST_PARTY = new Set(REGISTRY.link_check.allowed_hosts);
const keepableLinks = (set) => [...set].filter((h) => {
  try { return /^https?:$/.test(new URL(h).protocol) && FIRST_PARTY.has(new URL(h).hostname); }
  catch { return false; }
});

function notRun(reason) {
  const rows = [];
  for (const vp of Object.keys(VIEWPORTS)) {
    rows.push({
      browser: 'safari', viewport: vp, ok: null, console_errors: null,
      failed_requests: null, notes: `not run - ${reason}`, links: [],
    });
  }
  return rows;
}

async function scanOne(driver, spec, vpName) {
  const problems = [];
  const links = new Set();
  const bcfg = spec.browser || {};
  const entry = (spec.wordpress || spec.vercel).url;

  await driver.get(entry);
  await sleep(SETTLE_MS);

  if (bcfg.expect_auth_wall) {
    const body = await driver.findElement(By.css('body')).getText().catch(() => '');
    const refused = /authenticat|log in to vercel|access denied/i.test(body);
    if (!refused) problems.push('AUTH WALL GONE: the crew app answered a normal visitor.');
    return row(vpName, problems, [], 'real Safari');
  }

  for (const a of await driver.findElements(By.css('a[href]'))) {
    const h = await a.getAttribute('href').catch(() => null);
    if (h) links.add(h);
  }

  const overflow = await driver.executeScript(
    'return [document.documentElement.scrollWidth, window.innerWidth];');
  if (overflow[0] > overflow[1] + 2) {
    problems.push(`horizontal overflow: ${overflow[0]}px of content in a ${overflow[1]}px window`);
  }

  if (bcfg.check_embed) {
    const frames = await driver.findElements(By.css('iframe'));
    if (!frames.length) {
      problems.push('no <iframe> on the page - the embed tag is gone');
    } else {
      const src = await frames[0].getAttribute('src').catch(() => null);
      const expected = spec.wordpress?.expected_iframe_src;
      if (expected && src && src.replace(/\/$/, '') !== expected.replace(/\/$/, '')) {
        problems.push(`embed points at ${src} but should point at ${expected}`);
      }
      const rect = await frames[0].getRect().catch(() => null);
      const minH = bcfg.min_iframe_height ?? 300;
      if (!rect || rect.height < minH) {
        problems.push(`embed rendered ${rect ? Math.round(rect.height) : 0}px tall, under the ${minH}px floor - collapsed`);
      }
      // Switch INTO the embed. This is the whole point of the Safari lane: if Safari's
      // storage partitioning breaks the build, this is where it shows up as an empty frame.
      try {
        await driver.switchTo().frame(frames[0]);
        const inner = await driver.findElements(By.css('a[href]'));
        for (const a of inner) {
          const h = await a.getAttribute('href').catch(() => null);
          if (h) links.add(h);
        }
        const minLinks = bcfg.min_frame_links ?? 0;
        if (inner.length < minLinks) {
          problems.push(`only ${inner.length} links inside the embed in real Safari, expected at least ${minLinks}`);
        }
        const minControls = bcfg.min_frame_controls ?? 0;
        if (minControls) {
          const controls = await driver.findElements(
            By.css('a[href],button,[role="button"],input,select'));
          if (controls.length < minControls) {
            problems.push(`only ${controls.length} controls inside the embed in real Safari, expected at least ${minControls}`);
          }
        }
        // textContent, not the rendered text - same reason as browser_scan.mjs: these
        // headings are split across elements and CSS-uppercased.
        if (bcfg.frame_proof_string) {
          const text = await driver.executeScript(
            'return document.body ? document.body.textContent : "";').catch(() => '');
          if (!String(text).includes(bcfg.frame_proof_string)) {
            problems.push(`"${bcfg.frame_proof_string}" is missing from inside the embed in real Safari`);
          }
        }
      } catch (e) {
        problems.push(`could not read inside the embed in Safari: ${e.message.split('\n')[0]}`);
      } finally {
        await driver.switchTo().defaultContent().catch(() => {});
      }
    }
  }

  return row(vpName, problems, keepableLinks(links), 'real Safari');
}

function row(vpName, problems, links, engine) {
  return {
    browser: 'safari',
    viewport: vpName,
    ok: problems.length === 0,
    console_errors: null,   // safaridriver exposes no console log; null, never 0
    failed_requests: null,
    notes: [engine, problems.join(' | ')].filter(Boolean).join(' - '),
    links,
  };
}

async function main() {
  const targets = [...REGISTRY.targets].sort((a, b) => a.order - b.order);
  const out = { generated_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), targets: {} };
  for (const t of targets) out.targets[t.id] = { browsers: [] };

  let driver;
  try {
    driver = await new Builder()
      .forBrowser('safari')
      .setSafariOptions(new safari.Options())
      .build();
  } catch (e) {
    const reason = `safaridriver would not start (${e.message.split('\n')[0]}). ` +
      'On a macOS runner this usually means `sudo safaridriver --enable` did not run.';
    console.error(reason);
    for (const t of targets) out.targets[t.id].browsers = notRun(reason);
    write(out);
    return;   // exit 0 on purpose: "could not check" is a valid, honest report
  }

  try {
    for (const [vpName, [w, h]] of Object.entries(VIEWPORTS)) {
      await driver.manage().window().setRect({ width: w, height: h }).catch(() => {});
      for (const t of targets) {
        let r;
        try {
          r = await scanOne(driver, t, vpName);
        } catch (e) {
          r = { browser: 'safari', viewport: vpName, ok: false, console_errors: null,
                failed_requests: null, notes: `real Safari - scan crashed: ${e.message.split('\n')[0]}`, links: [] };
        }
        out.targets[t.id].browsers.push(r);
        console.log(`${r.ok ? 'ok  ' : 'FAIL'} safari/${vpName} ${t.id}${r.ok ? '' : ' :: ' + r.notes}`);
      }
    }
  } finally {
    await driver.quit().catch(() => {});
  }
  write(out);
}

function write(out) {
  mkdirSync(join(HERE, 'data'), { recursive: true });
  writeFileSync(join(HERE, 'data', '_safari.json'), JSON.stringify(out, null, 2) + '\n');
  console.log('wrote data/_safari.json');
}

main().catch((e) => { console.error(e); process.exit(1); });
