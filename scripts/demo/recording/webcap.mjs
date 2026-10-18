// Picky judges' flow, captured at 1920x1080 for the final video (Home suggestions -> order -> /watch with the camera
// switcher -> approvals / photo checks -> Visa payment agent -> receipt), with reading pauses.
//
//   node webcap.mjs                       # full run: PLACES A REAL ORDER on the gateway the app points at
//   WEBCAP_DRY=1 node webcap.mjs          # dry run: stops right BEFORE clicking "Order these" (no trip created)
//
// Two captures of the same session:
//   1. Playwright recordVideo (1920x1080 webm, VP8 ~1 Mbit/s: the fallback)  -> <out>/_video/*.webm
//   2. CDP screencast JPEG q WEBCAP_JPEG_Q (default 92) frames with timestamps -> <out>/frames + webcap_hq_1080p30.mp4
//      (CFR 30 fps, libx264 crf 16: what edit_final.py uses)
// Env: APP_URL (http://localhost:3001) PICKS (3) DETOUR_ANSWER (no) EXTRAS_ANSWER (none) VERIFY_WRONG (0)
//      WEBCAP_OUT (./webcap-<ts>) WEBCAP_TIMEOUT_S (1200) WEBCAP_READ_S (2.5: pause before each answer)
//      WEBCAP_VIEWS ("25:1:10,75:2:8,140:1:8" = seconds after /watch : Bird's-eye n : seconds shown, then Chase cam)
//      HEADED=1  PW_DIR (node_modules with playwright; default ../pw)  FFMPEG (default: imageio-ffmpeg via uv)
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PW_DIR = process.env.PW_DIR || path.join(HERE, "..", "pw");
const { chromium } = createRequire(path.join(PW_DIR, "package.json"))("playwright");

const E = process.env;
const APP = (E.APP_URL || "http://localhost:3001").replace(/\/$/, "");
const DRY = E.WEBCAP_DRY === "1";
const PICKS = +(E.PICKS || 3);
const DETOUR = E.DETOUR_ANSWER || "no";
const EXTRAS = E.EXTRAS_ANSWER || "none";
const VERIFY_WRONG = +(E.VERIFY_WRONG || 0);
const TIMEOUT_S = +(E.WEBCAP_TIMEOUT_S || 1200);
const READ_MS = 1000 * +(E.WEBCAP_READ_S || 2.5);
const JPEG_Q = +(E.WEBCAP_JPEG_Q || 92);
const VIEWS = (E.WEBCAP_VIEWS ?? "25:1:10,75:2:8,140:1:8")
  .split(",")
  .filter(Boolean)
  .map((s) => s.split(":").map(Number))
  .map(([at, n, dur]) => ({ at, n, dur, done: false }));
const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
const OUT = path.resolve(E.WEBCAP_OUT || path.join(HERE, `webcap-${DRY ? "dry-" : ""}${stamp}`));
fs.mkdirSync(path.join(OUT, "frames"), { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now() / 1000;
const events = [];
const ev = (kind, data = {}) => {
  const e = { t: +(Date.now() / 1000 - t0).toFixed(3), kind, ...data };
  events.push(e);
  console.log(`[${e.t.toFixed(1).padStart(6)}s] ${kind}`, Object.keys(data).length ? JSON.stringify(data) : "");
  fs.writeFileSync(path.join(OUT, "events.json"), JSON.stringify({ t0, app: APP, dry: DRY, events }, null, 1));
};

const browser = await chromium.launch({ headless: E.HEADED !== "1" });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1,
  recordVideo: { dir: path.join(OUT, "_video"), size: { width: 1920, height: 1080 } },
});
const page = await context.newPage();

// ---- CDP screencast: high-quality frames + timestamps -------------------------------------------------------------
const frames = [];
const cdp = await context.newCDPSession(page);
let lastSaved = 0;
cdp.on("Page.screencastFrame", async (f) => {
  const ts = f.metadata?.timestamp ?? Date.now() / 1000;
  const keep = ts - lastSaved >= 1 / 32; // <= ~30 saved frames/s (headless Chrome emits up to ~90/s)
  if (!keep) {
    try {
      await cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId });
    } catch {}
    return;
  }
  lastSaved = ts;
  const n = frames.length + 1;
  const file = `frames/${String(n).padStart(6, "0")}.jpg`;
  fs.writeFileSync(path.join(OUT, file), Buffer.from(f.data, "base64"));
  frames.push({ file, ts });
  try {
    await cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId });
  } catch {}
});
await cdp.send("Page.startScreencast", { format: "jpeg", quality: JPEG_Q, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1 });

async function clickAfterRead(loc, label) {
  await loc.scrollIntoViewIfNeeded().catch(() => {});
  await loc.hover().catch(() => {});
  await sleep(READ_MS);
  await loc.click({ timeout: 5000 });
  return label;
}

let outcome = "dry";
try {
  // ---- 1. Home: suggestion cards ------------------------------------------------------------------------------------
  await page.goto(APP + "/", { waitUntil: "domcontentloaded", timeout: 60000 });
  ev("home");
  let cards = page.getByRole("region", { name: /running low/i }).locator("button[aria-pressed]");
  try {
    await cards.first().waitFor({ state: "visible", timeout: 25000 });
  } catch {
    cards = page.locator("button[aria-pressed]");
    await cards.first().waitFor({ state: "visible", timeout: 5000 });
  }
  await sleep(3500); // card photos + let the viewer read Home
  const demo = (await page.getByText("Demo history", { exact: true }).count()) > 0;
  const n = await cards.count();
  ev("cards", { n, source: demo ? "DEMO fallback" : "gateway" });
  const inStore = [];
  const online = [];
  for (let i = 0; i < n; i++) ((await cards.nth(i).getByText("Online", { exact: true }).count()) ? online : inStore).push(i);
  const chosen = [...inStore, ...online].slice(0, Math.max(1, PICKS));
  const names = [];
  for (const i of chosen) {
    const c = cards.nth(i);
    await c.scrollIntoViewIfNeeded();
    await c.hover();
    await sleep(700);
    await c.click();
    names.push(((await c.getAttribute("aria-label")) || "?").split(",")[0]);
    await sleep(900);
  }
  ev("picked", { names });
  const btn = page.getByRole("button", { name: /order these/i });
  await btn.waitFor({ state: "visible", timeout: 5000 });
  await btn.scrollIntoViewIfNeeded();
  await btn.hover();
  await sleep(2500);
  ev("order_button_ready", { label: (await btn.innerText()).trim().replace(/\s+/g, " ") });
  await page.screenshot({ path: path.join(OUT, "before_order.png") });

  if (!DRY) {
    // ---- 2. order -> /watch -----------------------------------------------------------------------------------------
    await btn.click();
    ev("order_clicked");
    await page.waitForURL(/\/(watch|live|trip)(\?|\/|$)/, { timeout: 45000 });
    const u = new URL(page.url());
    ev("watch", { path: u.pathname, trip_id: u.searchParams.get("trip"), request_id: u.searchParams.get("request") });
    const tWatch = Date.now() / 1000;
    let viewUntil = 0;
    let lastAnswer = 0;
    let verifyWrong = 0;
    let unloadSeen = false;
    outcome = "timeout";
    const deadline = Date.now() / 1000 + TIMEOUT_S;
    while (Date.now() / 1000 < deadline) {
      const now = Date.now() / 1000;
      if (await page.getByRole("heading", { name: /^\s*Paid\b/ }).count()) {
        outcome = "receipt";
        ev("paid");
        const h = page.getByRole("heading", { name: /^\s*Paid\b/ }).first();
        await h.scrollIntoViewIfNeeded().catch(() => {});
        await sleep(7000); // receipt + confetti, readable
        await page.mouse.wheel(0, 500);
        await sleep(4000);
        await page.screenshot({ path: path.join(OUT, "receipt.png") });
        break;
      }
      if (await page.getByText("The trip stopped.").count()) {
        outcome = "stopped";
        ev("trip_stopped");
        await sleep(4000);
        break;
      }
      // camera switcher: Bird's-eye n for dur seconds, then back to the chase cam
      if (viewUntil && now >= viewUntil) {
        const chase = page.getByRole("tab", { name: /chase cam/i });
        if (await chase.count()) {
          await chase.first().click().catch(() => {});
          ev("view", { tab: "Chase cam" });
        }
        viewUntil = 0;
      }
      for (const v of VIEWS) {
        if (!v.done && !viewUntil && now - tWatch >= v.at) {
          const b = page.getByRole("tab", { name: new RegExp(`bird.s-eye( ${v.n})?$`, "i") });
          if (now - tWatch > v.at + 60) v.done = true; // tabs never showed up for this slot: give up on it
          if (await b.count()) {
            v.done = true;
            const tab = (await b.count()) > 1 ? b.nth(Math.min(v.n - 1, (await b.count()) - 1)) : b.first();
            await tab.click().catch(() => {});
            ev("view", { tab: (await tab.innerText().catch(() => "Bird's-eye")).trim() });
            viewUntil = now + v.dur;
          } else if (v.done) ev("view_skipped", { tab: v.n, reason: "no Bird's-eye tab (no ceiling cameras / robot not assigned)" });
        }
      }
      // approval dialogs
      const d = page.getByRole("alertdialog").first();
      if (now - lastAnswer > 1.5 && (await page.getByRole("alertdialog").count()) && (await d.isVisible())) {
        const text = (await d.innerText()).replace(/\s+/g, " ").slice(0, 160);
        const b = (rx) => d.getByRole("button", { name: rx }).first();
        let kind = "question";
        let ans = null;
        try {
          if (await d.getByRole("button", { name: /approve and pay/i }).count()) {
            kind = "checkout";
            ev("checkout_card", { text });
            ans = await clickAfterRead(b(/approve and pay/i), "Approve and pay");
          } else if (await d.getByRole("button", { name: /no thanks|yes, add it/i }).count()) {
            kind = "detour";
            ans = await clickAfterRead(b(DETOUR === "no" ? /^no thanks$/i : /yes, add it/i), DETOUR);
          } else if (await d.getByRole("button", { name: /^yes$/i }).count()) {
            ans = await clickAfterRead(b(/^yes$/i), "yes");
          } else {
            kind = "choice";
            const none = d.getByRole("button", { name: /^(no thanks|none|none of these|nothing|skip|not now|no)\b/i }).first();
            if (EXTRAS === "none" && (await none.count())) ans = await clickAfterRead(none, "none");
            else ans = await clickAfterRead(d.getByRole("button").first(), "(first option)");
          }
        } catch (e) {
          ans = `click failed: ${e.message.split("\n")[0]}`;
        }
        ev("approval", { kind, answer: ans, text });
        lastAnswer = Date.now() / 1000;
        continue;
      }
      // photo checks ("Check each pick")
      const panel = page.getByRole("region", { name: /purchase checks/i }).first();
      if (now - lastAnswer > 1.2 && (await page.getByRole("region", { name: /purchase checks/i }).count())) {
        const ok = panel.getByRole("button", { name: /looks right/i }).first();
        if ((await ok.count()) && (await ok.isVisible())) {
          const title = (await panel.locator("h3").first().innerText({ timeout: 1000 }).catch(() => "")).replace(/\s+/g, " ").slice(0, 60);
          const wrong = verifyWrong < VERIFY_WRONG;
          try {
            await clickAfterRead(wrong ? panel.getByRole("button", { name: /wrong item/i }).first() : ok, "");
            if (wrong) verifyWrong++;
            ev("photo_check", { item: title, answer: wrong ? "wrong" : "looks right" });
          } catch (e) {
            ev("photo_check_failed", { item: title, error: e.message.split("\n")[0] });
          }
          lastAnswer = Date.now() / 1000;
          continue;
        }
      }
      if (!unloadSeen && (await page.getByTestId("unload-text").count())) {
        unloadSeen = true;
        ev("unload", { text: (await page.getByTestId("unload-text").first().innerText().catch(() => "")).replace(/\s+/g, " ") });
      }
      await sleep(400);
    }
  }
} catch (e) {
  outcome = `error: ${e.message.split("\n")[0]}`;
  ev("error", { message: e.message.split("\n")[0] });
  await page.screenshot({ path: path.join(OUT, "error.png") }).catch(() => {});
} finally {
  ev("end", { outcome });
  await cdp.send("Page.stopScreencast").catch(() => {});
  const tEnd = Date.now() / 1000;
  const video = page.video();
  await context.close();
  await browser.close();
  const webm = video ? await video.path().catch(() => null) : null;
  // ---- CFR 30 fps H.264 from the screencast frames (ffconcat durations = real timing) ------------------------------
  const out = path.join(OUT, "webcap_hq_1080p30.mp4");
  if (frames.length) {
    const lines = ["ffconcat version 1.0"];
    frames.forEach((f, i) => {
      const next = i + 1 < frames.length ? frames[i + 1].ts : tEnd;
      lines.push(`file '${f.file}'`, `duration ${Math.max(0.001, next - f.ts).toFixed(4)}`);
    });
    lines.push(`file '${frames[frames.length - 1].file}'`);
    fs.writeFileSync(path.join(OUT, "frames.ffconcat"), lines.join("\n") + "\n");
    let ff = E.FFMPEG;
    if (!ff) {
      const r = spawnSync("uv", ["run", "--no-project", "--with", "imageio-ffmpeg", "python", "-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"], { encoding: "utf8" });
      ff = r.stdout.trim().split("\n").pop();
    }
    const r = spawnSync(ff, ["-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", path.join(OUT, "frames.ffconcat"),
      "-vf", "fps=30,scale=1920:1080:flags=lanczos:in_range=pc:out_range=tv,format=yuv420p", "-color_range", "tv", "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-movflags", "+faststart", out], { cwd: OUT, encoding: "utf8" });
    if (r.status !== 0) console.error("ffmpeg failed:", r.stderr);
  }
  const stats = {
    out: OUT,
    outcome,
    dry: DRY,
    seconds: +(tEnd - t0).toFixed(1),
    screencast_frames: frames.length,
    screencast_fps: frames.length > 1 ? +((frames.length - 1) / (frames[frames.length - 1].ts - frames[0].ts)).toFixed(2) : 0,
    hq_mp4: fs.existsSync(out) ? out : null,
    webm,
  };
  fs.writeFileSync(path.join(OUT, "stats.json"), JSON.stringify(stats, null, 1));
  console.log(JSON.stringify(stats, null, 1));
}
