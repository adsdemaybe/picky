# picky: research and stack decisions (DubHacks 2026)

> **Update 2026-10-17 (evening):** the T-Mobile track is **back in** (this reverses the earlier same-day note that
> dropped it). Side tracks: T-Mobile "Cart Whisperer" (design: `docs/cart-whisperer.md`), Photon 2.0, SideShift. Check
> the sponsor-track limit (§5).
>
> **Update 2026-10-17 (night):** reconciled with the teammate's web app (`arm/frontend`, PR #12) and their master plan
> `docs/team-plan.md` (on that branch until #12 merges): T-Mobile rubric and requirements from the team's copy of the
> brief (§6.2), a T-Mobile-style wireless store, SKIP / WAIT and zero suggestions in Cart Whisperer, the online
> "decision" layer (Find & compare, verdicts; §6.1), and the frontend stack as shipped (§9.10, re-checked against the
> 18:13 PT redesign `6fc1c41`).

Synthesized 2026-10-17 from six research reports (payments/trust, Photon, agent stack, hackathon context,
Isaac Sim, perception/data, routing) and two adversarial fact-check passes.

- **⚠️ marks an unverified claim.** The note after it says what to check. Do not put a ⚠️ item on a slide or
  build a critical path on it until someone has checked it.
- Fact-check corrections are already applied in the text. §13 lists what changed and gathers every ⚠️ item in one place.
- Versions are what researchers saw on PyPI or npm on 2026-10-17. Once `uv.lock` and `pnpm-lock.yaml` exist, the
  lockfiles are the source of truth.
- Paths follow the repo layout in `CLAUDE.md`: `arms/<arm>/`, `contracts/`, `utils/`, `data/planograms/`, `scripts/`.

---

## 0. TL;DR: the stack in 10 lines

1. **Python 3.12 + uv**, one project with per-arm optional extras. FastAPI gateway with an in-process event bus and
   websockets. Next.js 16 + Tailwind 4 + zustand in `arms/frontend` (as shipped; §9.10). Contracts flow Pydantic ->
   OpenAPI -> TypeScript.
2. **LLM:** `claude-sonnet-5-5` is the agent brain, using a hand-written tool loop on the `anthropic` SDK.
   `claude-haiku-5-5` handles voice turns, vision and classification. Model IDs live in env vars.
3. **Data:** a planogram YAML is the single source of truth. It generates the USD scene, the Postgres seed and the
   occupancy grid. The database is local Docker `pgvector/pgvector:pg17` (Supabase optional). Embeddings use local
   `bge-small-en-v1.5` (Voyage optional).
4. **Sim:** Isaac Sim runs as a separate sim-worker process on the GPU host "vhagar" (decided; a preinstalled 6.x
   build, `CLAUDE.md` §9). The rented-GPU rows in §2 (Brev first) are backups only. **A mock/2D SimBackend gets built
   first** so the Macs never wait on the GPU.
5. **Robot:** a small rover + vertical lift + SO-101 arm + basket (`CLAUDE.md` §3, `contracts.common`; this replaced
   the original Ridgeback-Franka research pick, kept in §9.4 as history). Navigation is kinematic set-pose plus pure
   pursuit. The demo picks with a "magic grasp" (attach/reparent). Real grasping (VLA on a physical SO-101) is later.
6. **Routing:** rasterize the planogram into a grid, build the distance matrix with skimage MCP / scipy Dijkstra, then
   solve with OR-Tools `RoutingModel`. List items are mandatory. Recommendations are disjunctions whose drop penalty
   scales with predicted interest.
7. **Perception:** zxing-cpp barcode read, then SigLIP 2 embedding match against the rendered label textures, then
   Claude vision. A ground-truth bypass is always available. YuNet face blur with `opencv-python-headless<5`.
8. **Trust/payments:** a deterministic AP2-style mandate engine decides (never the LLM), then Stripe test mode charges
   `pm_card_visa`. Visa Intelligent Commerce: apply for access and ship an adapter stub. Visa TAP-style RFC 9421
   request signing.
9. **Messaging (Photon):** Python `photonhq-api` 0.5.0 polling the shared iMessage line (the user texts first). A
   spectrum-ts Node sidecar is optional, for polls and tapbacks. Telegram is the backup channel.
10. **Voice:** Deepgram (Flux/Nova-3 STT + Aura-2 TTS, $200 free credit) with push-to-talk. Browser Web Speech is the
    zero-key fallback. **Submissions are due Sun Oct 18, 10:30 AM, so freeze code by about 8 AM.**

---

## 1. Do these NOW (hour 0)

Ask first: **does anyone have an NVIDIA RTX 4080+ (16 GB) machine running Linux or Windows 11?** The answer
decides §2.

Ordered by lead time, longest or unknown first.

| # | Action | Where | Lead time | Notes |
|---|---|---|---|---|
| 1 | Deploy the **NVIDIA Brev Isaac launchable** as a trial | [launchable](https://brev.nvidia.com/launchable/deploy/now?launchableID=env-35JP2ywERLgqtD0b0MIeK1HnF46) | Signup takes minutes. The first build "can take a while" (⚠️ 15-45 min is unverified) | Docs give no Isaac version, GPU type or price. Record `pip show isaacsim`, the GPU and the $/hr once it is up. Stop it when idle. |
| 2 | **Photon beta**: org, project, API key (`project:read`+`project:write`, shown once), assign a shared iMessage number, then **every teammate and demo phone texts it "ping"** | [photon.co/beta](https://photon.co/beta) -> [app.beta.photon.codes](https://app.beta.photon.codes) | ⚠️ No waitlist is documented, but this is not confirmed | The shared line cannot send the first message. Copy the Project ID (`pho_prj_...`). |
| 3 | **Visa Developer** account, create a project, click "Request access" on Visa Intelligent Commerce / Visa Token Service | [developer.visa.com](https://developer.visa.com) | Account is instant. VIC approval time is unknown, so assume it **will not land this weekend** | Ask the Visa table for hackathon sandbox credentials. Never on the critical path. |
| 4 | **Read the rules in a real browser** (the Notion pages are JS-rendered and unreadable to tools): dh26 page, DubHacks Hacker Guide, Photon guidelines. Ask the Visa / T-Mobile / SideShift / NVIDIA / Anthropic tables about rules, rubrics and credits | [dh26](https://dh26.dubhacks.co), [Hacker Guide](https://dubhacks.notion.site/), [Photon guidelines](https://somethingreat.notion.site/Hackathon-Guidelines-for-Photon-2-0-3f8e443f36bb81f9893fed0d08a4c01d) | 30 min | Team size, pre-existing-code rule, Devpost URL, track rubrics and the number of tracks you may enter are all unverified. |
| 5 | (Only if AWS is a GPU backup) Service Quotas -> EC2 -> "Running On-Demand G and VT instances" >= 8 vCPU | [quotas](https://console.aws.amazon.com/servicequotas/home/services/ec2/quotas) | Hours to days; can be denied | Third choice. Skip if Brev or RunPod works. |
| 6 | **Anthropic Console**: org, API key, add funds, set a spend limit, check limits | [platform.claude.com](https://platform.claude.com/) | Minutes | ⚠️ New orgs may start in a lower "Evaluation" tier with undocumented limits. Check `platform.claude.com/usage/limits`. No DubHacks credits are confirmed. |
| 7 | **Stripe** account (test mode) | [register](https://dashboard.stripe.com/register) | 5 min | `sk_test_` key. This is the critical-path checkout. Test mode needs no business verification. |
| 8 | **Deepgram** | [signup](https://console.deepgram.com/signup) | Instant | $200 credit, no card (per [pricing](https://deepgram.com/pricing)). |
| 9 | **GitHub** repo (make it public: free branch protection, and ⚠️ judges likely want it public), CI + PR template, every teammate on **Devpost** (⚠️ Devpost is assumed; confirm the platform), create the draft project Saturday evening | [github.com/new](https://github.com/new), [devpost.com](https://devpost.com) | Minutes | Commit early and visibly (a "code written during the event" rule is likely ⚠️). |
| 10 | **Pre-download on good network**: `docker pull pgvector/pgvector:pg17`; HF weights `timm/ViT-B-16-SigLIP2` and `BAAI/bge-small-en-v1.5`; YuNet `.onnx`; Open Food Facts Parquet | see §9 | 10-30 min | Venue Wi-Fi is a known risk. |
| 11 | Optional: **Visa Acceptance (Cybersource) sandbox** | [sandbox signup](https://developer.visaacceptance.com/hello-world/sandbox.html) | 20-60 min | Second, Visa-branded checkout rail. |
| 12 | Optional: **RunPod / Vast.ai / Lambda** account; **NVIDIA Developer Program** (for NGC container pulls and the AWS AMI) | [runpod.io](https://www.runpod.io/), [NVIDIA dev](https://developer.nvidia.com/developer-program) | Minutes | GPU backup. |
| 13 | Optional: **Telegram bot** via BotFather | [t.me/BotFather](https://t.me/BotFather) | 2 min | Messaging backup channel. |
| 14 | Optional: MongoDB Atlas (Voyage key), Kroger dev portal, Kaggle | see §9 | 5-10 min / ⚠️ unknown | Not needed for P0. |

---

## 2. Hardware plan

**Facts (confirmed).** Isaac Sim 6.2.0 runs only on Linux (Ubuntu 24.04; 22.04 is deprecated; glibc >= 2.35) or
Windows 11 (Windows 10 is unsupported). It needs an **RT-core NVIDIA GPU**: minimum RTX 4080 / 16 GB VRAM
(RTX 5080 recommended), 32 GB RAM minimum (64 GB recommended). **A100/H100 are unsupported** because they have no RT
cores. aarch64 works only on DGX Spark. macOS is not listed and the container is Linux-only, so **the Macs cannot run
Isaac Sim** (this is inferred from omission, which is enough to plan on).
[requirements](https://docs.isaacsim.omniverse.nvidia.com/latest/installation/requirements.html)

**Decision tree. Decide by hour 2.** Decided: the Isaac worker runs on the GPU host "vhagar" (`CLAUDE.md` §9); rows
1–3 are backups.

| Order | GPU host | How | Notes |
|---|---|---|---|
| 0 | Teammate's RTX 4080+ Linux/Win11 box | pip install (§9 sim) | Best latency. Same LAN as the demo laptop. |
| 1 | **NVIDIA Brev Isaac launchable** | One click. Browser VS Code plus streamed Isaac UI. "Using Secure Links" -> Shareable URL | Official ([docs](https://docs.isaacsim.omniverse.nvidia.com/latest/installation/install_advanced_cloud_setup_launchable.html)). ⚠️ Version, GPU, price and capacity are unknown until you deploy. |
| 2 | RunPod / Vast.ai / Lambda: **L40S, RTX 6000 Ada, RTX 4090, A10G or L4** | NGC container `nvcr.io/nvidia/isaac-sim`, `./runheadless.sh` | ⚠️ About $0.4-1.1/hr for L40S (third-party prices), so roughly $30-70 for the weekend. Pods may not allow `--network=host`, so use JPEG-over-websocket for the view. |
| 3 | AWS g6e with the NVIDIA Isaac Sim AMI | Only if the G/VT quota already exists | ⚠️ AMI 2026.3.0, g6e/g7e only, NICE DCV on :8443: from the Windows listing, unverified. |

- **All Mac-side work never touches the GPU.** Gateway, agent, routing, recommender, trust, messaging, frontend and
  perception (PyTorch MPS) all run on Apple Silicon against `arms/sim/fake.py`. Only the sim worker runs on the GPU box.
- **Network:** the sim worker connects *outbound* to the gateway (or FastAPI reaches it through an SSH tunnel). Stream
  JPEG frames over the websocket. Do **not** rely on WebRTC: it needs UDP 47998 + TCP 49100 + `--network=host`,
  and venue Wi-Fi often blocks UDP.
- **Gates:** if there is no GPU host at hour 2, skip Isaac and use the 2D sim. If Isaac does not render the planogram store with the
  robot by about **hour 8**, freeze Isaac and make the 2D view primary. **As soon as one Isaac trip works, screen-record it.**
- **No-GPU fallback, ranked:** (1) **2D top-down sim**: the SVG store map animated from the same A* path and
  planogram. It is built anyway as the mock, it is robust, and it is 2-3 h of work. (2) **MuJoCo** (`uv add mujoco mink`, native
  Apple Silicon, Menagerie Franka, `mujoco.Renderer` offscreen to JPEG) is real physics but 4-8 h of work. (3) Genesis
  (Metal) or ManiSkill3 (CPU on macOS): not recommended. If not on Isaac, the pitch says "simulated store", not "Isaac Sim".
- **Python versions:** Isaac 6.2 needs **Python 3.12** exactly (5.x was 3.11). `CLAUDE.md` §7 says Isaac's Python is
  3.11. That is true only for 5.x. Develop on 3.12 and keep code 3.11-compatible (no PEP 695) in case the GPU host ships 5.x.

---

## 3. Scope tiers (about 22 hours of real hacking)

| Tier | Must work in the demo |
|---|---|
| **P0** (end-to-end by hour 8-10) | Planogram -> DB seed + grid + SVG map. Agent intent expansion + tool loop. Route with detours (OR-Tools). Mandate engine + approval UI + Stripe test checkout. Cart/budget/chat UI. Fake robot moving along the route. Photon text-in list + text approval. |
| **P1** | Isaac scene + kinematic navigation + magic grasp + live JPEG feed. Recommender with "why". Push-to-talk voice. Merchant dashboard. Perception cascade on about 10 SKUs. Face blur. Post-purchase follow-up -> trip 2 smarter. Produce top-3 (ground-truth freshness). TAP-style signing. |
| **P2 / stretch** | Real RMPflow/cuMotion grasping. VLM freshness scoring. Batched shoppers. Third store. Cybersource rail. Stripe SPTs. iMessage polls/tapbacks. MuJoCo. Real-to-sim scan. Kroger enrichment. Photon voice prize. |

---

## 4. Top risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | **No GPU, or Isaac setup eats hours.** First launch compiles shaders and downloads caches (10-20+ min). Budget 1-3 h. | Build the fake SimBackend first. Deploy Brev at hour 0. Use the decision gates in §2. Record a video early. |
| 2 | **Scope:** 11 arms, about 22 h. | Follow the §3 tiers. Every arm ships `fake.py`. Cut P2 without guilt. |
| 3 | **Isaac 6.x API drift.** Lula/motion_generation, `isaacsim.robot.manipulators` and `isaacsim.robot.wheeled_robots` Python controllers are deprecated. Most blog/LLM snippets are 4.x/5.x. | Pin the 6.2.0 docs. Use kinematic set-pose navigation and magic grasp, which need no controller APIs. Read extension source on the box when needed. |
| 4 | **Photon beta is two days old.** Docs are partial. The shared line cannot text first and has no groups or calls. | The demo user texts first. Use the Python polling path. Keep a Terminal/Telegram backup. Record a video. |
| 5 | **Visa VIC access will not land.** mTLS / X-Pay / MLE can burn hours. | Stripe test mode on the critical path, a `VisaIntelligentCommerceAdapter` stub, and TAP-style signing. Label everything "sandbox / test mode". |
| 6 | **Venue Wi-Fi** (cloud LLM, STT/TTS, UDP, big downloads). | Local Postgres. Pre-downloaded weights. JPEG over WS. Push-to-talk + Web Speech fallback. Phone hotspot. |
| 7 | **Rules and rubric unverified** (Notion unreadable). | Read in a browser at hour 0. Ask the sponsor tables. |
| 8 | **LLM in the payment path, or overclaiming**, would sink Trust (15%). | Only the mandate engine can call the payment function. Approvals are bound to a cart hash. Never say "certified" or "Visa-approved". |
| 9 | **Anthropic limits / spend-cap 429** (`enforced_spend_limit_reached` has no retry-after). | Fund tonight. Prompt caching. Haiku for chatty turns. `CLAUDE_MODEL_*` env fallback to `claude-sonnet-5` / `claude-haiku-4-5`. |
| 10 | **Integration breaks at merge time.** | Contracts-first PRs. Fakes. `make demo` in CI. Regenerate (never hand-merge) `uv.lock` and generated TS. |
| 11 | **Dependency clashes**: OpenCV 5 now resolves by default; Isaac bundles its own torch/OpenCV. | Pin `opencv-python-headless>=4.10,<5`. Run perception in the gateway process, not inside Isaac's Python. |
| 12 | **Silent routing bugs**: unit mismatch between cm arc costs and penalties; unreachable approach poses. | Two synthetic tests (high penalty -> visited, low -> dropped). Connected-component check at startup. |

---

## 5. Hackathon logistics

- **When and where:** DubHacks 2026, Sat Oct 17 - Sun Oct 18, University of Washington. Theme "Let's Get Cookin'",
  "$44,000+ in prizes", 1,000+ students. Hacker applications are closed.
  [dh26.dubhacks.co](https://dh26.dubhacks.co), [dubhacks.co](https://dubhacks.co)
- **Saturday:** check-in 9:30-11:00, **opening ceremony 11:00-12:30**, lunch 12:30-2:30, workshops 1:30-4:30, career
  fair 2:30-4:00, dinner 6-7.
- **Sunday:** breakfast 9-10:30, **SUBMISSIONS DUE 10:30 AM**, **JUDGING 11:30 AM-2:00 PM**, lunch 1-2:30, closing
  2:15-3:05. That leaves about **22-23.5 h of hacking**, not the 24-36 h in the original brief.
- **Tracks on dh26:** Visa ("Reimagine Shopping"; ⚠️ the name is inferred from an image filename, and the page has no
  description), Community Kitchen, Chef's Choice, Cook Smarter. **T-Mobile, Photon and SideShift tracks are not on the
  dh26 page** ⚠️. They are known from the team brief and sponsor lists only.
- **Unverified ⚠️:** submission platform (Devpost assumed) and URL, team-size cap, pre-existing-code rule, judging format
  (expo tables or timed demos), per-track rubrics. Last year's
  [Hacker Guide](https://dubhacks.notion.site/The-DubHacks-24-Hacker-Guide-16da323fc21b4c6c8c0c2fbca1b7755e) said code must
  be uploaded before the deadline and linked on Devpost, and that teams could enter **up to two sponsored tracks plus
  the title track**. That is a prior-year rule; confirm it, because we now list three side tracks (T-Mobile, Photon, SideShift) and it
  may force a choice of two. The cap is an **open team decision**; if a cut is forced, the team plan's default priority
  is Visa → T-Mobile → Photon → SideShift (it would drop SideShift).
- **Plan:** code freeze **8:00 AM Sunday**. Submit by 10:00. Rehearse the demo 3 times. Have a 30-second version for
  short judging slots. Contact: hello@dubhacks.co.

---

## 6. Judging map

### 6.1 Visa "Reimagine Shopping" rubric (⚠️ the weights come from the team brief; no public Visa page matched them, so re-read the track page)

| Criterion | Weight | Demo moments / features | Arms |
|---|---|---|---|
| **Innovation** | 25% | A shopper-owned agent that works across stores. An orienteering route: every list item plus interest-weighted optional detours, re-solved after each yes/no. Split sourcing (in-store -> robot, OOS -> online) with one checkout. A second trip that is visibly smarter. The decision stage online: Find & compare across fictional retailers with Good price / Wait / Skip it verdicts (web app on demo data; optional 15 s demo beat). | routing, agent, recommender, catalog |
| **UX** | 25% | One-tap or "yes" approval in iMessage. Push-to-talk voice in store. Live SVG map with budget bar and "why this" cards. Top-3 freshest produce picker. Approval modal with itemized total. | frontend, messaging, agent |
| **Technical Execution** | 20% | Planogram -> USD scene + DB seed + grid. Grid Dijkstra + OR-Tools. Agent tool loop with approval gates. Live sim feed over WS. Barcode/embedding/VLM cascade. Recorded fallback if the sim crashes. | all |
| **Commerce Impact** | 15% | Merchant dashboard (out-of-stocks, mispricings, shelf heatmap from robot scans). Near-expiry markdowns routed only to likely buyers. Basket lift and accepted-suggestion rate against a seeded baseline (labelled simulated). | catalog, recommender, frontend |
| **Trust & Responsibility** | 15% | **Live** APPROVE / NEEDS_APPROVAL / DENY from the deterministic mandate engine. Approval bound to the cart hash. Signed mandate chain and audit log in the UI. TAP-style signed agent requests. Face blur on the feed. "Why suggested / no paid placement". Revoke / kill switch. "Sandbox" labels. | trust, perception, frontend |

Evidence from older Visa-run hackathons: they rewarded a live working demo, named sponsor tools and visible UX polish.
One published rubric gave a point per viable Visa API use ([Money20/20 2018](https://developer.visa.com/pages/money2020-2018)).
Trust only scores if it is **visible in the UI**.

### 6.2 Side tracks

| Track | What we show | Status |
|---|---|---|
| **Photon 2.0** | Messaging arm built on Photon and live on iMessage (shared line), with Telegram as a second channel. Pre-trip list by text, mid-trip approval requests, post-purchase follow-up that updates the taste profile ("context persists across time and channels"). | Qualification is "built on Photon 2.0 + live on >= 1 Photon channel" (team brief / Notion). ⚠️ Prizes ($700 iMessage winner, $300 runner-up, $400 Best Voice Agent) and judging (Vision, Craft, Depth, Traction) come from the Notion page and may describe an earlier hackathon; confirm. |
| **T-Mobile "Cart Whisperer: checkout suggestions that actually help"** (entered) | Cart Whisperer (`docs/cart-whisperer.md`, recommender arm) + the brief's "bring the one that says wait". The brief is about phone shopping, so T-Mobile judging shows a T-Mobile-style wireless store (`store_tmobile`). From commerce, customer (owned items, device age, installments left, household lines) and real-time physical signals it shows ≤3 explained cards per phase (planning, in-trip, before you pay), zero is valid, with explicit SKIP and WAIT. Deterministic score + reason codes; the LLM only phrases. Accepts go through the mandate engine. Show accepted rate, $ saved, avoided spend. Designed, not implemented in the backend (needs contracts v0.3); the web app shows planning SKIP / WAIT on demo data. | ⚠️ Rubric per the team's copy of the brief (`UW_Hackathon_Challenge_Brief.docx`, `docs/team-plan.md` §3.2): Customer value 30%, Use of signals 25%, Explainability & trust 20%, Feasibility & demo 25%. Requirements: name the direction, a phone-shopping store in the demo, ≥1 SKIP or WAIT live, "synthetic data only" on a slide. Re-read the docx; prizes unverified. |
| **SideShift "Best Startup Idea"** | One business-model slide: free for shoppers. Grocers pay SaaS for shelf intelligence and personalized basket lift. **No pay-to-play placement.** 2x2 landscape (retailer-owned vs shopper-owned, passive scan vs active plan/approve). | ⚠️ Track text unverified. |

---

## 7. Demo storyline (two trips, about 3 minutes)

> Historical research draft. The current script is [`docs/demo-script.md`](demo-script.md) (Maya, the taco trip, and
> the wireless variant for T-Mobile).

1. **Trip 1 (about 90 s).** Alex texts the Photon number "tacos for 6, under $40, one vegetarian". The agent expands it
   into items, quantities and substitutions. One item is out of stock in store, so it goes to the online order. The map
   shows the list-only route next to the route with 2 recommendation detours ("+12 m, 25 s"). At a detour the robot
   asks: "Pepper-jack? 30% off, near expiry, you rated a similar cheese 5 stars." Alex says yes by voice. The robot
   picks, drops, and the camera verifies the basket. A $60 add-on goes above "ask me above $X", so the mandate engine
   returns NEEDS_APPROVAL and an approval goes to iMessage. Then Stripe sandbox checkout. A follow-up text asks "How was
   the pepper-jack?", and the profile updates.
2. **Trip 2 (about 60 s).** Same list. The route re-plans: the rejected detour is gone and a favorite comes earlier.
   The budget bar shows the savings and a "what changed since last time" panel shows the profile change.
3. **Merchant flip.** Shelf heatmap and out-of-stock alerts built from the robot's scans during trip 1.
4. **Close.** "The shopper owns the agent, the merchant gets the shelf intelligence, and the mandate keeps it trustworthy."

If Isaac is unstable, play the recorded sim run as the "live view" while route, agent, mandate and messaging run
for real. Say so honestly.

---

## 8. Pitch stats and landscape (⚠️ none of these were re-checked by the fact-checkers; open each source before putting it on a slide, and always show source and year)

| Claim | Source | Year |
|---|---|---|
| 7.4% of sales lost to out-of-shelf, $82B | [NielsenIQ OSA barometer](https://www.businesswire.com/news/home/20220207005098/en/NielsenIQ-Launches-On-Shelf-Availability-OSA-Barometer-for-U.S.-Retailers) | 2021 data |
| Shoppers hit an out-of-stock on about 17.8% of trips (do not combine with the NielsenIQ figure) | [IHL via RetailTouchPoints](https://www.retailtouchpoints.com/topics/store-operations/ihl-group-ids-biggest-losers-when-it-comes-to-out-of-stock-failures) | older |
| 57% add unplanned items every time or most times | [Advantage/Suzy](https://youradv.com/advantage360/spontaneous-sales-why-grocery-shoppers-are-making-unplanned-purchases/) | Jan 2021 |
| Impulse buys in 69% of goal-less trips vs 24% of routine shops | [Breda study](https://www.foodingredientsfirst.com/news/grocery-shopping-impulse-purchases-study.html) | n=323 |
| 71% expect personalization, 76% frustrated without it | [McKinsey](https://www.mckinsey.com.br/en/our-insights/what-is-personalization) | - |
| US wasted 73.9M tons of food (about a third of supply) | [ReFED via Total Retail](https://www.mytotalretail.com/article/how-dynamic-markdowns-can-help-mitigate-food-waste/) | 2023 |
| Dash Cart users spend 10% more (company-reported) | [GeekWire](https://www.geekwire.com/2026/amazon-rolls-out-new-version-of-its-dash-smart-grocery-cart/) | 2026 |

Do **not** use: "87% of grocery waste is from expiry" (unverified) or McKinsey "10-15% revenue lift" (not confirmed).

**Landscape (for the 2x2):** Amazon Dash Cart (new version in 2026, sold to third-party grocers). Amazon scaled back
Just Walk Out in US grocery (2024 reporting; it is still used in some venues). Instacart Caper (acquired for about $350M in
[2021](https://techcrunch.com/2021/10/19/instacart-acquires-caper-ai-a-smart-cart-and-instant-checkout-startup-for-350m-as-it-moves-deeper-into-physical-retail-tech);
⚠️ "100+ cities, 15 states, Weis Markets June 4 2026"). Simbe Tally (⚠️ Tally 4.0 on Jan 12 2026; 3,000+ units
contracted Sep 21 2026, [Shelby Report](https://theshelbyreport.com/2026/09/21/simbe-surpasses-3000-units-in-shelf-intelligence-fleet/)).
Walmart and Bossa Nova is the cautionary tale (do not state an exact end date). All of these are **retailer-owned**:
they scan, but they do not route or act within a user's mandate across stores. Agentic-commerce protocols (Visa VIC/TAP,
Google AP2/UCP, OpenAI ACP) handle online checkout. **Pitch line: nobody has solved the physical aisle.** ⚠️ Do not
claim OpenAI Instant Checkout is live or retired (sources conflict), or that TAP is GA (the "June 2026" date is unconfirmed).

---

## 9. Arms

### 9.0 contracts / utils / monorepo tooling

| | |
|---|---|
| **Pick** | One **uv** project at the repo root (`pyproject.toml`, one `uv.lock`, dev Python 3.12, `requires-python=">=3.11"`). Packages are `contracts`, `utils` and `arms.*`. Heavy deps go in per-arm extras (`perception`, `voice`, `sim`). The frontend uses pnpm in `arms/frontend`. |
| **Fallback** | uv **workspace** (`[tool.uv.workspace] members=["arms/*","contracts","utils"]`, `{ workspace = true }` sources). Use it only if arms need real dependency isolation. Costs: one shared `requires-python` and one lockfile anyway. |
| **Contracts flow** | Pydantic in `contracts/` -> FastAPI `/openapi.json` -> `openapi-typescript` 7.13 -> `arms/frontend/src/lib/api-types.ts`. Websocket events are not in OpenAPI, so export `TypeAdapter(Event).json_schema()` and run it through `json-schema-to-typescript`, or add a dummy `GET /_events` with `response_model=Event`. `make gen-types`. |

Setup:
```bash
uv init --python 3.12 && uv add pydantic pydantic-settings fastapi 'uvicorn[standard]' websockets httpx
uv add --dev ruff pytest pytest-asyncio
uv add --optional perception open-clip-torch torch zxing-cpp 'opencv-python-headless>=4.10,<5' pillow
npx openapi-typescript http://localhost:8000/openapi.json -o arms/frontend/src/lib/api-types.ts
```
- **CI** (`.github/workflows/ci.yml`): `astral-sh/setup-uv` -> `uv sync --frozen --all-extras` (or skip torch in CI) ->
  `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`. Node 22 + pnpm -> `tsc --noEmit` + lint.
  Stale-type check: `make gen-types && git diff --exit-code`.
- **utils/** holds only cross-arm, feature-agnostic code: `config` (pydantic-settings: model IDs, `ROBOT_MODE`,
  `PICKY_REAL_ARMS`), `log`, `events` (EventBus), `fsm`, `grid` (world<->cell `Grid` dataclass shared by routing,
  robot and frontend), `llm` (client, caching, model routing, retries), `money` (integer cents), `signing`
  (HMAC/Ed25519, RFC 9421 helpers), `voice` (`stt_stream` / `tts` interface), `images` (JPEG encode).
- **contracts/** holds: `events` (discriminated union on `type`), `planogram`, `catalog`, `route`, `mandate`, `messaging`,
  `sim` (SimBackend commands: `navigate_to`, `pick`, `place`, `scan`, `snapshot`), and the `EMBED_DIM` constant.

Gotchas:
- A top-level package named `utils` can collide with a third-party `utils`. Never add a dependency that installs one.
  Run tools from the repo root.
- openapi-typescript v7 emits `components['schemas']['X']`. Pydantic discriminated unions become `oneOf` +
  discriminator, which it handles.
- Branch protection is free on **public** repos (private needs GitHub Team). Otherwise rely on convention plus the PR template.
- `uv.lock` and the generated TS **will** conflict between arm branches. Regenerate them; never hand-merge.

Sources: [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/), [FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/)

### 9.1 catalog

| | |
|---|---|
| **Pick** | `data/planograms/store_*.yaml` (fixtures, slots, SKUs, origin) feeds `scripts/build_store.py`, which emits (a) the Postgres seed, (b) the USD scene spec, (c) the occupancy grid. **Hour 1:** a hand-written ~60-SKU catalog with fake brands and PIL-rendered labels with EAN-13 barcodes, so every arm is unblocked. **Then:** enrich with an Open Food Facts subset (200-500 US products via DuckDB). Prices, stock, expiry and deals are synthetic. 2-3 stores plus an "online" store in one `inventory` table keyed by `store_id`. |
| **Fallback** | The hand-written catalog only (no licensing questions). |
| **Store API** | `GET /products/search`, `/products/{sku}/location?store=`, `/stores/{id}/deals`, `/stores/{id}/near-expiry`, plus a multi-store search that unions stores and online. Scan updates go to `POST /scans` (location, stock, freshness). |

Setup:
```bash
uv add duckdb pandas pillow zxing-cpp sqlmodel 'psycopg[binary]' pgvector
# ⚠️ dump URLs not re-fetched by the fact-checker; inspect the schema first: DESCRIBE SELECT * FROM 'food.parquet'
curl -L -o data/off/food.parquet 'https://huggingface.co/datasets/openfoodfacts/product-database/resolve/main/food.parquet?download=true'
```
`select code, product_name, brands, categories_tags, image_front_url from read_parquet('food.parquet') where list_contains(countries_tags,'en:united-states') and image_front_url is not null limit 500`
(⚠️ check the column names with DESCRIBE first).

Gotchas:
- OFF live API limits (confirmed): **15 req/min product reads, 10 req/min search per IP**. Global limits return HTTP 503
  and bans are possible. Use dumps only, never live calls in the demo. Send a custom User-Agent.
- ⚠️ Licences: ODbL (database), DbCL (contents), CC BY-SA (images). Add an attribution line in the README and footer.
  Do not call the textures proprietary.
- Use `image_front_url`; many OFF photos have glare or angles. OFF has no prices.
- Kroger API (optional price/aisle enrichment, run offline once): ⚠️ everything is unverified. The `filter.locationId`
  -> `aisleLocations` shape (`bayNumber`, `number`, `shelfNumber`, `side`) comes from third-party samples. The
  10,000/day limit is third-party. The `product.compact` scope and 1,600/day locations limit are unconfirmed. The
  approval wait is unknown. **Never route on it.** The planogram is the truth. [developer.kroger.com](https://developer.kroger.com)

Sources: [OFF data](https://world.openfoodfacts.org/data), [OFF API](https://openfoodfacts.github.io/openfoodfacts-server/api/), [OFF licence](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/license-be-on-the-legal-side/)

### 9.2 perception

| | |
|---|---|
| **Pick (identify)** | A cascade on each crop: (1) `zxingcpp.read_barcodes(img)` on the EAN-13/QR rendered on box faces, then (2) open_clip **SigLIP 2** (`hf-hub:timm/ViT-B-16-SigLIP2`) cosine match against the gallery of **your own label textures**, then (3) Claude vision (`claude-haiku-5-5`, or Sonnet if accuracy lags) choosing from a candidate list. Crop with the sim's `bounding_box_2d_tight` annotator. |
| **Pick (freshness top-3)** | P1: a **ground-truth freshness** float per produce instance in the planogram (texture variants fresh/ripe/bruised). P2: one multi-image Claude call returning JSON ranks (`Candidate 1:` labels, images before text), blended 0.6/0.4 with an HSV hue/brown-pixel heuristic. |
| **Pick (basket verify)** | At the drop event, take an overhead basket camera frame, crop per slot, run the same cascade, then multiset-diff against the cart. Emit `basket_verified{expected, observed, mismatches}`. |
| **Pick (face blur)** | OpenCV **YuNet** `cv2.FaceDetectorYN` with `face_detection_yunet_2023mar.onnx` and Gaussian blur on boxes padded about 20%. Blur **before** a frame is streamed, stored or sent to an LLM. |
| **Fallback** | A ground-truth bypass: prim -> SKU from the sim, emitted as scan results. Keep real CV live on about 10 SKUs. For CLIP use open_clip `ViT-B-32` / `laion2b_s34b_b79k`. For faces use MediaPipe Tasks `FaceDetector` (BlazeFace `.tflite`), or ground-truth blur of NPC head boxes. For OCR on the Mac only, `ocrmac` (Apple Vision). |

Setup:
```bash
uv add --optional perception zxing-cpp open-clip-torch torch pillow 'opencv-python-headless>=4.10,<5'
# SigLIP 2 needs open-clip-torch >= 2.31 and timm >= 1.0.15 (⚠️ from mirrors/model cards); device 'mps' on Mac, else cpu
curl -L -o models/face_detection_yunet_2023mar.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
```
YuNet: `cv2.FaceDetectorYN.create(model, "", (w, h), score_threshold=0.7, nms_threshold=0.3, top_k=5000)`, then
`setInputSize((w, h))` per frame and `detect(img)`, which returns N x 15 (box, 5 landmarks, score).

Gotchas:
- **Pin OpenCV < 5.** PyPI's latest is `opencv-python-headless` 5.0.0.93 (confirmed). ⚠️ That 2023mar breaks on 5.x is
  unverified, so the pin is a precaution. A dynamic `2026may` YuNet exists (inputs must be multiples of 32) if you go to 5.x.
- YuNet is trained on real faces and may miss low-poly sim faces. Test early and use textured face billboards / NPCs.
- Claude vision (confirmed): max 8000x8000 px, 10 MB base64 per image, 32 MB per request, 100 images per request (200k-context
  models), a 2000 px cap above 20 images, a high-res tier (2576 px) on 4.7+ / 5.x models, and the Files API to avoid resending.
  It is **weak under 200 px, counting is approximate, coordinates are approximate.** Never count identical items with it.
- Rendered barcodes: at least ~300 px wide with a quiet zone and no glancing angles. Test at the real camera distance. Use
  zxing-cpp, not pyzbar (needs the system zbar; no release since 0.1.9).
- ⚠️ SigLIP 2 MPS speed is unbenchmarked. Run perception in the gateway process, **not** inside Isaac's Python.

Sources: [Claude vision](https://platform.claude.com/docs/en/build-with-claude/vision), [zxing-cpp](https://pypi.org/project/zxing-cpp), [open_clip models](https://mintlify.com/mlfoundations/open_clip/usage/pretrained-models), [YuNet 2026may mirror](https://huggingface.co/pollen-robotics/face_detection_yunet_2026may), [MediaPipe face](https://developers.google.com/edge/mediapipe/solutions/vision/face_detector/python), [ocrmac](https://github.com/straussmaximilian/ocrmac)

### 9.3 sim

| | |
|---|---|
| **Pick** | **Isaac Sim 6.2.0** headless in a standalone **sim-worker** (`arms/sim/app.py`). It owns the main loop; a background thread runs a websocket server that **only enqueues** commands; the main loop drains the queue and steps non-blocking action state machines (NAVIGATING / PICKING / SCANNING). It publishes state and JPEG frames. The scene comes from the planogram with `pxr`. A `SimBackend` Protocol in `contracts/sim` has implementations `fake` (2D), `isaac`, and optionally `mujoco`. |
| **Fallback** | The 2D top-down fake as primary view, or MuJoCo (§2). A recorded video for demo day. |

Setup (Linux GPU box):
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128     # torch BEFORE isaacsim
pip install 'isaacsim[all,extscache]==6.2.0.0' --extra-index-url https://pypi.nvidia.com
export OMNI_KIT_ACCEPT_EULA=YES
pip install websockets opencv-python-headless numpy pydantic && pip install -e . --no-deps   # contracts/utils only
```
- Worker: `from isaacsim import SimulationApp; app = SimulationApp({"headless": True})`, imported **before** any omni or
  pxr import. Loop: `while app.is_running(): drain cmd_q; step action; world.step(render=True); every N frames publish JPEG`.
- **Scene from the planogram:** `Usd.Stage.CreateNew`, `UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)`,
  `SetStageMetersPerUnit(stage, 1.0)`. Each slot is a scaled `UsdGeom.Cube`/`Cylinder` with a `UsdPreviewSurface`
  bound to that SKU's label PNG. Shelves get `UsdPhysics.CollisionAPI` and items get `RigidBodyAPI` (low mass, CCD). Add `sku`/`price`
  custom attributes on prims, a ground plane, a dome light and a few rect lights.
- **Camera to web:** `rp = omni.replicator.core.create.render_product(cam_path, (960, 540))`;
  `ann = rep.AnnotatorRegistry.get_annotator("rgb"); ann.attach([rp])`; `ann.get_data()` returns RGBA; then face blur, then
  `cv2.imencode(".jpg", q=70)` over WS at 8-15 fps. The first frames are black, so warm up with a few `app.update()` calls.
- WebRTC (optional wow view): `./runheadless.sh -v`, TCP 49100 + UDP 47998, `--network=host`, compose web viewer on :8210.

Gotchas:
- **Never** call USD or physics from the server thread. Run a single SimulationApp per GPU.
- Isaac 6.x moved many APIs to `isaacsim.*.experimental` (including camera/sensors). Do not copy `omni.isaac.*` or 5.x snippets.
- Some NVIDIA assets are in centimeters (scale 0.01). Keep the scene in meters. Asset root is cloud-hosted, so the box needs internet.
- The `latest` docs URL moves with each release. Link `6.2.0` pages. Some 6.2.0 asset pages 404 while `latest` works.
- **Real-to-sim: skip.** ⚠️ Researchers found no tool named "CleanRoom3D" (a negative search only), so ask the teammate who
  suggested it. NuRec / 3DGRUT splats need Linux + CUDA + GCC <= 11, take hours, have no collisions, and are being
  deprecated in favor of ParticleField. Stretch only, after the demo works.
- There is no retail/grocery SimReady pack. The Warehouse collection (800+ assets: racks, shelves) is optional polish.

Sources: [install (pip)](https://docs.isaacsim.omniverse.nvidia.com/latest/installation/install_python.html), [container](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/installation/install_container.html), [6.0 migration](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/migration_guides/isaac_sim_6_0/index.html), [warehouse assets](https://docs.isaacsim.omniverse.nvidia.com/6.0.1/digital_twin/warehouse_logistics/tutorial_static_assets.html), [NuRec utils](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/assets/nurec_utils.html), [Marble workflow](https://developer.nvidia.com/blog/simulate-robotic-environments-faster-with-nvidia-isaac-sim-and-world-labs-marble)

### 9.4 robot

> **Superseded (history).** The team switched to a small rover + vertical lift (0.2–1.2 m) + SO-101 arm + basket
> (`CLAUDE.md` §3 and §9, `contracts.common`). The Ridgeback-Franka research below is kept for reference only. The
> navigation, magic-grasp and 6.x-deprecation notes still apply.

| | |
|---|---|
| **Pick (asset)** | **Clearpath Ridgeback-Franka** at `Clearpath/RidgebackFranka/ridgeback_franka.usd`, relative to the `Isaac Sim/Robots` asset root (multiphysics variant: `ridgeback_franka.usda` under `Robots_Multiphysics`). It has a holonomic base. Parent a basket (cube-shell prims with colliders) to the base link with a fixed drop pose. |
| **Pick (navigation)** | Pure-Python **pure pursuit** on the smoothed route (§9.5), output to a **kinematic set-pose** of the base each physics step (`set_world_pose`), with cosmetic wheel spin. Stop and settle (v < 0.02), then rotate to the approach yaw before picking. Stow the arm while driving. |
| **Pick (picking)** | **Magic grasp** for the demo: script the end effector along an arc (above shelf, down, up, basket drop pose), then attach the item (`UsdPhysics.FixedJoint` to `panda_hand`, or reparent + disable gravity), then reparent it to the basket. Target poses come from the planogram/DB as ground truth. |
| **Stretch** | The shipped `isaacsim.robot_motion.examples` pick-place (`standalone_examples/api/isaacsim.robot_motion.examples/manipulation/pick_place.py --robot franka`) on **cuMotion** RMPflow (`isaacsim.robot_motion.cumotion`). Surface gripper: `isaacsim.robot.surface_gripper` (`create_surface_gripper`, `close_gripper`, `get_gripped_objects`; ⚠️ no explicit open call is documented). |
| **Fallback** | A Franka (`FrankaRobotics/FrankaPanda/franka.usd`) on a kinematic box base with a visual 4-wheel mesh. |

Setup: `assets_root = get_assets_root_path()` (`isaacsim.storage.native`), then
`add_reference_to_stage(assets_root + "/Isaac/Robots/Clearpath/RidgebackFranka/ridgeback_franka.usd", "/World/Robot")`.
⚠️ Check that the `/Isaac/Robots/...` prefix matches the stage browser, and read the actual prim and joint names (arm base,
`panda_hand`) before configuring anything.

Gotchas:
- **6.x deprecations (confirmed):** `isaacsim.robot_motion.lula` and `motion_generation` are replaced by
  `isaacsim.robot_motion.experimental.motion_generation` + `cumotion` + `pink`. `isaacsim.robot.manipulators.examples`
  is replaced by `isaacsim.robot_motion.examples`. **`isaacsim.robot.wheeled_robots` Python controllers are deprecated.** They
  moved to `isaacsim.robot.experimental.wheeled_robots`, and the OmniGraph nodes stay in `isaacsim.robot.wheeled_robots.nodes`.
  New class names are not documented, so read the source on the box. RMPflowCortex registrations were removed in 6.2.
- cuMotion needs CUDA and XRDF/URDF configs. Those exist for Franka/UR10, probably not for the combined Ridgeback-Franka. Plan with an
  arm-only model and a stationary base. Check that `reset()` is OK before `forward()`, and treat `None` outputs as invalid.
- Physical grasping of small boxes is flaky. Magic grasp is the demo path.
- ⚠️ Fetch and TIAGo were reported absent from the current catalog (not rechecked). Spot-with-arm, Ridgeback-UR5 and Galbot G1 also exist.

Sources: [mobile manipulators](https://docs.isaacsim.omniverse.nvidia.com/latest/assets/usd_assets_robots_mobile_manipulator.html), [motion migration](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/migration_guides/isaac_sim_6_0/robot_motion_to_experimental_motion_generation.html), [control migration](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/migration_guides/isaac_sim_6_0/robot_control_extensions_to_experimental.html), [Franka pick-place](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/examples/manipulation_franka_pick_place.html), [surface gripper API](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/py/source/extensions/isaacsim.robot.surface_gripper/docs/api.html)

### 9.5 routing

| | |
|---|---|
| **Pick (grid)** | Rasterize the planogram with `skimage.draw.polygon` (shelves, counters, pillars, outside of the store outline) at **0.05 m**, or 0.10 m if the matrix is slow. Inflate with `scipy.ndimage.distance_transform_edt` (`free = clearance >= robot_r + 0.05`). Use one `utils.grid.Grid(origin_xy, res, shape)` with `world_to_cell` / `cell_to_world` everywhere. |
| **Pick (matrix + paths)** | One `skimage.graph.MCP_Geometric(cost, fully_connected=True).find_costs([src])` per source, indexing all targets from that cost array (N runs, not N^2). Use cost 1 on free cells for true meters. Path = `traceback`, then line-of-sight shortcut smoothing and resampling at about 0.25 m. Cache by planogram hash. |
| **Pick (solver)** | **OR-Tools** (`ortools==9.15.*`, `pywrapcp.RoutingModel`), one vehicle, `RoutingIndexManager(N, 1, [entrance], [checkout])`. List items get **no disjunction** (mandatory). Each recommendation gets `AddDisjunction([idx], penalty)`. Optional `Dist` dimension as a walking budget. `PATH_CHEAPEST_ARC` + `GUIDED_LOCAL_SEARCH`, `time_limit` 1-2 s. **Re-solve after each yes/no** from the current node. |
| **Fallback** | Greedy cheapest-insertion of rec stops onto a 2-opt mandatory tour (~40 lines). `pathfinding` 1.0.22 / networkx A* for single paths. A cached demo route JSON. |

Setup and core:
```python
# uv add 'ortools==9.15.*' numpy 'scipy>=1.18' scikit-image pathfinding     (scipy 1.18 needs Python >= 3.12)
from ortools.constraint_solver import pywrapcp, routing_enums_pb2 as enums

SCALE = 100  # meters -> integer cm; penalties MUST use the same unit


def plan(D, optional, penalty_m, start, end, max_m=None, secs=2):
    Di = (D * SCALE).round().astype(int).tolist()
    mgr = pywrapcp.RoutingIndexManager(len(Di), 1, [start], [end])
    r = pywrapcp.RoutingModel(mgr)
    cb = r.RegisterTransitCallback(lambda i, j: Di[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
    r.SetArcCostEvaluatorOfAllVehicles(cb)
    if max_m:
        r.AddDimension(cb, 0, int(max_m * SCALE), True, "Dist")
    for n in optional:
        r.AddDisjunction([mgr.NodeToIndex(n)], int(penalty_m[n] * SCALE))
    p = pywrapcp.DefaultRoutingSearchParameters()
    p.first_solution_strategy = enums.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    p.local_search_metaheuristic = enums.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    p.time_limit.FromSeconds(secs)
    s = r.SolveWithParameters(p)
    if not s:
        return None  # e.g. max_m too small for the mandatory stops
    route, i = [], r.Start(0)
    while not r.IsEnd(i):
        route.append(mgr.IndexToNode(i))
        i = s.Value(r.NextVar(i))
    dropped = [n for n in optional if s.Value(r.NextVar(mgr.NodeToIndex(n))) == mgr.NodeToIndex(n)]
    return route + [mgr.IndexToNode(i)], dropped
```
- **Penalty mapping:** `penalty_m = interest x K` means "meters of detour this item is worth" (for example K = 15 gives
  interest 0.8 -> 12 m). The official docs warn that penalties smaller than the detour cost let the solver drop more. Agree
  K with the recommender. Cap recommendation stops at 3-5 per trip by passing only the top-K candidates.
- **Batched shoppers (P2):** still one vehicle, over the union of stops. Dedupe identical SKUs into one node with
  `users`. A node is mandatory if any user listed it. A shared recommendation's penalty is the sum of interested users' penalties. Optional basket
  volume via `AddDimensionWithVehicleCapacity`. Per-user budgets belong to trust, not routing.
- **Detour cost for the UI:** cost with the stop minus cost without it ("+12 m, 25 s").

Gotchas:
- **Products sit inside shelf cells (obstacles)**, so routing to them returns inf. Define an **approach pose** per slot: the nearest
  free cell on the aisle side (about 0.6-0.8 m off the face) with yaw facing the shelf. Assert every approach pose is in the
  entrance's connected component (`scipy.ndimage.label`).
- Replace inf with a big int, or drop unreachable nodes. Symmetrize: `D = (D + D.T) / 2`. Merge zero-distance duplicates.
- Aisles must be wider than 2 x (robot_r + margin); design them >= 1.5 m. Inflate by the footprint circumradius including the stowed arm.
- numpy uses `[row, col] = [y, x]`. World x maps to col. Flip y only for display.
- ⚠️ Do not use `penalty=-1` to mean mandatory (that is not in the docs); simply add no disjunction. ⚠️ ortools 9.15.6755 was released on
  Jan 14 2026, so check for a newer release. ⚠️ Solve times (1-5 s for 30-150 nodes) and MCP timings are estimates; measure them.
- Isaac `omap` is optional cross-checking only: `isaacsim.asset.gen.omap.bindings._omap.Generator(physx, stage_id)`,
  `update_settings(cell_size, occupied, unoccupied, unknown)`. **The real defaults are 0.05 / 1.0 / 0.0 / 0.5** (the 4/5/6
  in the docstring is only illustrative). Then `set_transform(origin, min_bound, max_bound)`, `generate2d()`, `get_buffer()`. Its origin is the
  region **center**.
- ⚠️ The scikit-image 0.26 (Python >= 3.11) and networkx 3.7 (>= 3.12) pins were not rechecked.

Sources: [OR-Tools penalties](https://developers.google.com/optimization/routing/penalties), [routing options](https://developers.google.com/optimization/routing/routing_options), [CVRP](https://developers.google.com/optimization/routing/cvrp), [ortools PyPI](https://pypi.org/project/ortools/), [skimage.graph](https://scikit-image.org/docs/stable/api/skimage.graph.html), [csgraph](https://docs.scipy.org/doc/scipy/reference/sparse.csgraph.html), [omap 6.2.0](https://docs.isaacsim.omniverse.nvidia.com/6.2.0/py/source/extensions/isaacsim.asset.gen.omap/docs/index.html)

### 9.6 agent

| | |
|---|---|
| **Pick (models)** | `claude-sonnet-5-5` ($2 / $10 per MTok) for the agent, intent expansion and rerank with "why". `claude-haiku-5-5` (from $0.10 / $0.50) for voice turns, label reading and classification. `claude-opus-5-5` ($4 / $20) only for hard planning or as an eval judge. **Skip** `claude-fable-5-1` ($10 / $50). All have 1M context and 128K max output. Retirement is no sooner than Sep-Oct 2027. |
| **Pick (loop)** | A **hand-written tool loop** on the async `anthropic` client with streaming and `strict: true` tools: `query_inventory`, `plan_route`, `recommend`, `add_to_cart`, `request_approval`, `checkout`. Cap at about 8 iterations. `request_approval` emits `approval.requested` on the bus and awaits an `asyncio.Future` (resolved by the web UI or Photon) with a timeout. **Mandate checks live in Python inside `add_to_cart` / `checkout`**, so the LLM cannot bypass them. |
| **Intent expansion** | `client.messages.parse(..., output_format=ShoppingList)` -> `.parsed_output` (items, qty, substitutions, budget). Split sourcing happens in code: an in-store stock check sends each item to the robot or to the online order. |
| **Fallback** | `client.beta.messages.tool_runner` with `@beta_async_tool` for a quick first version. Model fallback through the env vars `CLAUDE_MODEL_SMART` / `CLAUDE_MODEL_FAST`, to `claude-sonnet-5` / `claude-haiku-4-5` / `claude-opus-5` (separate rate-limit buckets). |
| **Not** | The Claude Agent SDK. It embeds the Claude Code binary, aims at file/command agents, and adds a subprocess and built-in tools. |

Setup: `uv add anthropic` (1.15.0). Keys and model IDs go in `utils.config`, and all calls go through `utils.llm`.
Gotchas:
- **Prompt caching:** put `cache_control` on the system prompt, tool definitions and catalog context. Cache reads cost **5%** of input
  on Opus/Sonnet 5.5, **10%** on Haiku 5.5 and 2.5% on Fable. Cached reads do not count toward ITPM. The Batch API is 50% off (fine for precomputing recommendations).
- **Effort:** thinking is adaptive and steered by `effort`. Defaults: Sonnet `high`, Haiku `medium`, **Opus 5.5 `medium`**. Set
  low effort on voice turns. Do **not** send the old `thinking: {type: "enabled", budget_tokens}`.
- **Strict schemas:** no numeric min/max, no min/maxLength, no recursion, `additionalProperties: false`, max **20 strict
  tools**, 24 optional params, 16 union params. The first use compiles the schema, so warm it at startup. Changing `output_config.format`
  invalidates the cache. In `client.beta.messages.create()` use `output_config`, not `output_format` (TypeError).
- **Limits:** the Start tier is 1,000 RPM / 2M ITPM / 400K OTPM for the 5.5 models, with a $500/month cap. ⚠️ New orgs may start lower in an
  "Evaluation" tier. Spend-cap 429s have no retry-after.

Voice (in the agent arm, using the `utils.voice` interface):

| | |
|---|---|
| **Pick** | **Deepgram**: streaming STT (Flux for end-of-turn detection, or Nova-3) + **Aura-2** TTS from the backend. Browser mic (MediaRecorder/AudioWorklet, 16 kHz PCM) -> `/ws/voice` -> Deepgram. Stream Haiku's reply sentence by sentence into TTS. `uv add deepgram-sdk` (7.13.0). |
| **Fallback** | Browser Web Speech: `webkitSpeechRecognition` (Chrome/Edge, HTTPS or localhost; audio goes to Google) + `speechSynthesis`. Zero keys, about 10 min of work. Then Cartesia (20K credits/month free). |
| **Gotchas** | Pricing per Deepgram's page: Flux $0.39/hr, Nova-3 $0.29/hr, Aura-2 $0.030 per 1K chars. ⚠️ Latency figures are third-party, so test on venue Wi-Fi. Ignore the unconfirmed "Flux TTS". **Push-to-talk**, and mute the mic during TTS (echo). Safari Web Speech is flaky. |

Sources: [models overview](https://platform.claude.com/docs/en/about-claude/models/overview), [rate limits](https://platform.claude.com/docs/en/api/rate-limits), [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [tool runner](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-runner), [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), [Deepgram pricing](https://deepgram.com/pricing), [Cartesia pricing](https://cartesia.ai/pricing)

### 9.7 recommender

| | |
|---|---|
| **Pick** | Local **sentence-transformers** `BAAI/bge-small-en-v1.5` (384-dim, no key, works offline on MPS/CPU). Embed `name \| brand \| category \| tags \| description` per product into pgvector `vector(384)`. The profile vector is a weighted mean of liked/purchased vectors minus disliked ones. Cosine top-k, then **Sonnet rerank** of the top few with a "why you'd like this" line. This produces the `interest` that routing turns into `penalty_m`. Cap 3-5 rec stops per trip. Cart Whisperer (`docs/cart-whisperer.md`) replaces the "want any of these?" batch: at most 3 cards per phase (planning, in-trip, before you pay), SKIP and WAIT included, zero allowed. Show near-expiry markdowns only when a user's similarity is above a threshold. |
| **Fallback / upgrade** | **Voyage `voyage-4-lite`** (1024-dim by default; `input_type="document"` for products, `"query"` for profiles; normalized vectors). The key comes from a **MongoDB Atlas** model API key: `VOYAGE_API_KEY`, endpoint `https://ai.mongodb.com/v1`, `voyageai >= 0.3.7`. ⚠️ Free tokens and pricing are unverified ([billing](https://www.mongodb.com/docs/voyageai/management/billing/)). For about 500 products an exact numpy scan needs no index. |
| **Post-purchase** | Haiku parses the follow-up reply into structured ratings, which update the profile and its vector. Trip 2 then re-plans. |

Setup: `uv add sentence-transformers pgvector` (6.1.0 / 0.5.1). For Voyage: `uv add voyageai` (0.5.0) and
`vo.embed(texts, model="voyage-4-lite", input_type="document")`. In SQL:
`create extension if not exists vector; create index on products using hnsw (embedding vector_cosine_ops);` and query with `<=>`.

Gotchas: Anthropic has **no embedding model**. Fix `EMBED_DIM` once in `contracts/` before seeding, and never mix
models in one column. An index is only used when the query operator matches its op class. pgvector HNSW supports `vector` up to 2,000 dims
(`halfvec` up to 4,000, pgvector >= 0.7). Every recommendation stores its reason and signals (Trust).

Sources: [Anthropic embeddings](https://platform.claude.com/docs/en/build-with-claude/embeddings), [Supabase HNSW](https://supabase.com/docs/guides/ai/vector-indexes/hnsw-indexes), [voyageai PyPI](https://pypi.org/project/voyageai)

### 9.8 trust (mandates + payments)

| | |
|---|---|
| **Pick (engine)** | Pure-Python, deterministic, Pydantic models after AP2: **IntentMandate** (user-set budget cap, allowed categories, `ask_above_cents`, expiry, merchants), **CartMandate** (items, prices, store/online split, hash), **PaymentMandate** (amount, instrument ref = Stripe `pm_...`, hash of intent+cart, approval record). Signed with HMAC or Ed25519 (PyNaCl) and stored in Postgres as an audit log. Returns `APPROVE` / `NEEDS_APPROVAL` / `DENY` with reason codes. The LLM only *proposes* carts. |
| **Pick (checkout)** | **Stripe test mode**: `stripe.PaymentIntent.create(amount=total_cents, currency="usd", payment_method="pm_card_visa", confirm=True, automatic_payment_methods={"enabled": True, "allow_redirects": "never"})`. It is called only by the engine, after APPROVE and a recorded approval bound to the cart hash. The receipt carries the PaymentIntent id and the mandate hash. |
| **Pick (Visa story)** | `VisaIntelligentCommerceAdapter` stub (`enroll_card`, `request_agent_token`, `create_payment_instruction`, `send_commerce_signal`) backed by Stripe, labelled "VIC-ready, sandbox access pending". **TAP-style agent identity:** the agent signs outbound requests to our Store API with **RFC 9421** HTTP Message Signatures, and the API verifies them against an agent registry. |
| **Fallback** | A single signed Mandate JSON + hash chain. A mock `/checkout` receipt. |
| **Optional** | **Visa Acceptance (Cybersource) sandbox**: `cybersource-rest-client-python` 0.0.80, `POST /pts/v2/payments` on `apitest.cybersource.com`, `authenticationType=http_signature`. **Stripe SPTs**: `POST /v1/shared_payment/issued_tokens`, `Stripe-Version: 2026-09-30.preview`, `usage_limits` (currency, `max_amount`, `expires_at`), revoke, plus `return_url`. |

Setup: `uv add stripe pynacl`. Signup at [Stripe](https://dashboard.stripe.com/register). Test card **4242 4242 4242 4242**, and
`pm_card_visa` server-side (both confirmed on [Stripe testing](https://docs.stripe.com/testing)). Unit tests: over cap,
disallowed category, above `ask_above`, expired mandate, approval replay, cart changed after approval.

Gotchas:
- **Money is integer cents.** The LLM has no tool that reaches the payment function. Approvals come from an authenticated UI or
  Photon reply tied to `cart_hash`.
- ⚠️ The "Stripe blocks raw PAN API calls" claim is unverified, but use `pm_...` tokens anyway. The PaymentIntent params are standard but were not re-fetched.
- **VIC:** no self-serve sandbox was found, and agents are "onboarded". mTLS, X-Pay-Token and MLE can each burn hours. There is no Python SDK
  ([visa/ai](https://github.com/visa/ai) is TypeScript). Docs are readable through the Visa docs MCP at `https://sandbox.mcp.visa.com/mcp/doc`. developer.visa.com blocks scrapers, so open it in a browser.
- **Visa Acceptance Agent Toolkit** (confirmed): pilot, TypeScript, Node 18+, needs an MID + REST key + Invoicing/Pay-by-Link enabled
  by an account manager. **Not usable from Python. Skip it.**
- **TAP repo** ([visa/trusted-agent-protocol](https://github.com/visa/trusted-agent-protocol), confirmed): Agent Registry :8001
  (Python), Merchant Backend :8000 (FastAPI), CDN Proxy :3002 (Node), Merchant Frontend :3001 (React), TAP Agent :8501
  (Streamlit). It is a 5-service sample, not a library, so copy only the sign/verify logic. ⚠️ Ed25519 is **not stated** on the repo
  page; check the source for the algorithm. ⚠️ Licence/"TAP Product Terms" not read.
- **AP2** ([repo](https://github.com/google-agentic-commerce/AP2)): no PyPI release, `uv pip install git+https://github.com/google-agentic-commerce/AP2.git@main`,
  Python 3.11+. Models live under `code/sdk/python/ap2/models/`. ⚠️ The Intent/Cart/Payment trio is not listed in the README; check
  `ap2/sdk/README.md` before naming it on a slide. Samples are Gemini-based, so don't adopt them.
- **Cybersource:** the PyPI metadata declares no `requires_python` and has classifiers for Python 2.7 / 3.5-3.8 only. It is old generated code, so
  **smoke-test on 3.12 early**. ⚠️ Whether test PAN 4111 1111 1111 1111 works in the sandbox is unverified; read the
  [testing guide](https://developer.visaacceptance.com/hello-world/testing-guide.html) in a browser.
- **SPTs** need the agentic-commerce preview terms, `stripe.preparePaymentMethod` from the Payment Element, and work only in the US, Canada and select EU countries.
  The docs now point to `npm install -g @stripe/cli` + `stripe agent setup`. Optional only.
- **Never** claim certification, production tokens or "Visa-approved". Label everything sandbox.
- **Trust ledger screen** (the 15%): tokenization (the card never touches the robot or LLM), consent (intent mandate plus per-purchase
  approval), control (caps, categories, revoke, kill switch), transparency (why suggested, no paid placement), privacy (face
  blur, deletable preferences), auditability (signed chain plus post-purchase "commerce signal"), agent identity (signed requests).
  Optional: a WebAuthn passkey step-up as a stand-in for Visa Payment Passkey (about 1 h).

Sources: [Stripe agentic commerce](https://docs.stripe.com/agentic-commerce), [SPT](https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens.md?agent-seller=agent), [VIC](https://developer.visa.com/capabilities/visa-intelligent-commerce), [TAP](https://developer.visa.com/capabilities/trusted-agent-protocol), [Acceptance toolkit](https://developer.visaacceptance.com/docs/vas/en-us/agent-toolkit/quick-start/all/na/agent-toolkit/agent-toolkit-intro.html), [cybersource PyPI](https://pypi.org/pypi/cybersource-rest-client-python/json), [Visa MCP server](https://corporate.visa.com/en/sites/visa-perspectives/innovation/visa-mcp-server-agent-acceptance-toolkit.html)

### 9.9 messaging (Photon 2.0)

| | |
|---|---|
| **Pick** | **Python-only first**: `photonhq-api` **0.5.0** (`import photon_api`; `Photon` / `AsyncPhoton`; Python 3.11+). An asyncio task polls `projects.list_messages` about every 2 s, dedupes on message id, and posts normalized `InboundMessage`s to the gateway. Replies go through `projects.send_message` with an **Idempotency-Key** (= approval id). This is the path the beta iMessage guide documents. |
| **Upgrade (optional)** | A **spectrum-ts** Node/Bun sidecar in `arms/messaging/sidecar/` with no business logic: `for await (const [space, message] of app.messages)`, `space.send()`, native **polls**, reactions/replies, and a persistent connection (no tunnel). It also has a Terminal provider (`spectrum-ts/providers/terminal`) for offline dev. Use it when polling latency hurts or you want polls and tapbacks. |
| **Fallback** | Telegram via BotFather (`@spectrum-ts/telegram`, `SPECTRUM_TELEGRAM_BOT_TOKEN`; counts as a Photon channel if built on Spectrum), then a plain Telegram bot behind the same interface. A web chat uses the same agent endpoints (`channel: imessage \| telegram \| terminal \| web`). |

Setup:
1. At [app.beta.photon.codes](https://app.beta.photon.codes), create an org and project, then copy the Project ID (`pho_prj_...`) and an API key.
2. On the iMessage page, add your phone or Apple ID, then **Assign** a number from the shared pool.
3. **Text the number "ping" from every demo phone** (judges too).
4. `uv add photonhq-api`. ⚠️ Constructor and method signatures follow the [beta iMessage guide](https://photon.codes/docs/beta/get-started/imessage), so copy its snippet.
5. Account linking: the sender id is an E.164 phone number. The first text `link ABC123` uses a code from the web app.
6. Approval message: a short "why" line plus a plain "Reply YES or NO". Parse with a regex (`yes|yeah|y` / `no|n|skip`), with a timeout that defaults to skip.
   Correlate replies to the latest pending approval for that user, kept in Postgres.

Gotchas:
- **The shared line cannot send an unsolicited first message** (beta docs, confirmed). The stable spectrum-ts docs describe "50 new
  conversations/line/day", which implies first messages are possible there. ⚠️ Test with your actual credentials, and have the user text first regardless.
- Shared lines: **DMs only** (group `space.create` throws `UnsupportedError`), **no calls**, and recipients may see different sender
  numbers. Batched shoppers are separate DMs.
- Quotas (confirmed): 5,000 msgs/day per server and 50 new conversations/day per line. Increases via help@photon.codes. ⚠️ HTTP rate
  limit 5 req/s per project (report). ⚠️ Free plan "25 shared iMessage assignments" is unverified. ⚠️ "$100 credits" is from a secondary source.
- ⚠️ Whether spectrum-ts `projectId` / `projectSecret` come from the stable ([app.photon.codes](https://app.photon.codes)) or the beta
  dashboard, and whether the beta shared line works with spectrum-ts, is unknown. Resolve it in the first hour with a Terminal echo.
- Delivery is at-least-once, so **dedupe on message.id**. Sends are accepted asynchronously and are not delivery-confirmed.
- Polls and tapbacks: poll votes arrive as `content.type == "poll_option"` and reactions as `"reaction"` (`emoji`, `target.id`). The tapback-to-emoji
  mapping is undocumented and hosted poll delivery may lag, so **never depend on them alone**. Text YES/NO always works. Unknown
  `content.type` values go to a default branch.
- Webhooks (sidecar alternative, inbound only): register at `https://spectrum.photon.codes/projects/$ID/webhooks/` and verify
  `X-Spectrum-Signature` (`v0=` HMAC-SHA256 over `v0:{timestamp}:{rawBody}`, 5-minute window) on the **raw** body. Public HTTPS only.
- The beta docs are partly stubs, so expect drift. Ask in the Photon Discord (linked from the Notion guidelines). Skip the voice prize: it needs an SMS
  number and SIP setup, and ⚠️ SMS provisioning lead time is unknown.
- Never log full phone numbers in the repo.

Sources: [beta iMessage guide](https://photon.codes/docs/beta/get-started/imessage), [photon-hq/api changelog](https://github.com/photon-hq/api/blob/main/CHANGELOG.md), [spectrum-ts getting started](https://photon.codes/docs/spectrum-ts/getting-started), [iMessage routing](https://photon.codes/docs/spectrum-ts/providers/imessage/connection-and-routing), [native polls](https://photon.codes/docs/spectrum-ts/providers/imessage/messaging-features/native-polls), [webhooks](https://photon.codes/docs/webhooks/quickstart), [terminal provider](https://photon.codes/docs/spectrum-ts/providers/terminal), [Hermes Photon plugin](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/photon), [llms.txt](https://photon.codes/docs/llms.txt)

### 9.10 frontend

| | |
|---|---|
| **Pick** | **Next.js 16.4** (App Router, Turbopack; ⚠️ the 6 Oct 2026 release date is from the blog listing) + React 19 + **Tailwind 4.3** + **shadcn/ui** (CLI 4.22) + zustand + **Recharts 3.10**. **Store map:** hand-written SVG with a viewBox in store meters (`<g transform="scale(1,-1)">`). Shelves are `<rect>`s from the planogram, the route is a `<polyline>` (solid blue for mandatory legs, dashed amber for detour legs), and dropped recs are hollow markers. **Live view:** JPEG frames over WS into `<img>`/`<canvas>`. **Dashboard:** Recharts plus an SVG grid heatmap of shelves (stock/freshness). |
| **Fallback** | Vite + React. A pre-recorded `<video autoplay loop>` as the "live view". Leaflet `L.CRS.Simple` if you need pan/zoom. (Streamlit hurts the UX score.) |
| **As shipped** (teammate, `arm/frontend` @ `6fc1c41`) | Next.js 16.4.2, React 19.3, Tailwind 4, **shadcn/ui** (radix, CLI 4.22) + Lucide icons + motion + sonner on the team's own palette (`arms/frontend/DESIGN.md`), zustand 5, hand-drawn SVG charts and store map, `@anthropic-ai/sdk` for Ask Picky; **no Recharts**. Demo data from `data/*.yaml` via `scripts/gen-data.mjs`; the live feed has three labeled sources on the Live page (simulated cameras, gateway `/ws/frames`, a direct MJPEG / MP4 URL per camera). Where the pick above differs, the shipped app wins. |

Setup: `pnpm create next-app@latest arms/frontend --ts --tailwind --app --turbopack`, `pnpm dlx shadcn@latest init`,
`pnpm add recharts zustand`. One `useEventSocket()` hook connects to `ws://localhost:8000/ws`, narrows on `type`, and feeds a
zustand store used by Map, Cart, BudgetBar, ChatPanel, ApprovalModal and TrustLedger. Pin Node 22 LTS in `.nvmrc`.

Gotchas: connect to FastAPI directly (CORS for :3000). Do not proxy websockets through Next, and no Server Actions for robot
traffic. Recharts needs `"use client"`. Labels inside the y-flipped group render mirrored, so put text outside it. Simplify polylines
(Douglas-Peucker) before sending. Use route version numbers so stale routes are ignored. **No more than 2 h on map polish before P0 works.**

Sources: [Next.js blog](https://nextjs.org/blog), [Leaflet CRS.Simple](https://leafletjs.com/examples/crs-simple/crs-simple.html)

### 9.11 gateway

| | |
|---|---|
| **Pick** | **FastAPI 0.143** + uvicorn 0.54 + pydantic 2.14, fully async, **one process, one worker**. One `/ws` per client (cart, route, approvals, robot pose, frames). SSE (`fastapi.sse.EventSourceResponse`, available since 0.135) for LLM token streams. In-process `utils.events.EventBus` (topic -> set of `asyncio.Queue`). An explicit trip FSM: `INTENT -> LIST_CONFIRMED -> RECS -> ROUTE_PLANNED -> EXECUTING <-> AWAITING_APPROVAL -> CHECKOUT -> FOLLOWUP`. Long robot runs are lifespan-managed `asyncio.create_task`s (not `BackgroundTasks`). `PICKY_REAL_ARMS` / `ROBOT_MODE=fake` select real or fake arms. `GET /healthz`. |
| **DB** | **Local Docker** `pgvector/pgvector:pg17` + SQLModel (0.0.48) / SQLAlchemy 2 async + `psycopg[binary]` 3.3.6 + `pgvector`. `scripts/seed.py` drops and recreates (skip Alembic). |
| **Fallback** | Enum + transition-table FSM. Postgres LISTEN/NOTIFY only if you go multi-process. **Supabase hosted** through the same SQLAlchemy URL (not supabase-py). For the DB itself, SQLite + numpy cosine. |

Setup:
```bash
docker run -d --name picky-db -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg17
uv add fastapi 'uvicorn[standard]' sqlmodel 'psycopg[binary]' pgvector
uv run uvicorn arms.gateway.main:app --reload --port 8000
```
Gotchas:
- **Never use `--workers > 1`** (the bus is in-process). Wrap blocking work (OR-Tools solve, MCP, sync DB, CLIP) in `await asyncio.to_thread(...)`.
- Every WS message is JSON with a `type` discriminator. The sim worker connects as a WS client and is authenticated with a token.
- Supabase free plan (per the [pricing page](https://supabase.com/pricing)): 2 active projects, 500 MB, **pauses after 1 week idle**, 200 Realtime
  connections. ⚠️ A third-party blog claims a PostgREST grant change from Oct 30 2026. Do not mix supabase-py and SQLAlchemy on the same tables (RLS).
- Supabase CLI local dev pulls about a dozen images, so pre-pull or skip it.

Sources: [FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/), [Supabase pricing](https://supabase.com/pricing)

---

## 10. Version pins (seen 2026-10-17; the lockfile wins)

| Python (3.12) | Version | Node | Version |
|---|---|---|---|
| anthropic | 1.15.0 | next | 16.4 |
| fastapi / uvicorn / pydantic | 0.143.3 / 0.54 / 2.14 | tailwindcss | 4.3.3 |
| sqlmodel / psycopg / pgvector | 0.0.48 / 3.3.6 / 0.5.1 | shadcn (CLI) | 4.22.0 |
| ortools | 9.15.6755 (⚠️ check for newer) | openapi-typescript | 7.13.0 |
| scipy | 1.18.1 (needs Python >= 3.12) | recharts | 3.10.1 |
| scikit-image / networkx | 0.26 / 3.7 (⚠️ pins unverified) | spectrum-ts (optional) | latest; TypeScript 5+ |
| photonhq-api | 0.5.0 (Python 3.11+) | Node | 22 LTS |
| stripe / pynacl | latest | uv | 0.12.9 |
| deepgram-sdk | 7.13.0 | | |
| zxing-cpp / open-clip-torch | 3.1.1 / 3.3.0 | | |
| opencv-python-headless | **>=4.10,<5** | | |
| sentence-transformers / voyageai | 6.1.0 / 0.5.0 | | |
| cybersource-rest-client-python | 0.0.80 (old; test on 3.12) | | |
| isaacsim (GPU box only) | 6.2.0.0 + torch 2.11.0 (cu128) | | |

---

## 11. Suggested repo rules (for CLAUDE.md; already mostly there)

- One feature = one arm folder `arms/<arm>/` = one branch `arm/<arm>`, merged into `main` **only via PR**. Never push to `main`.
- **Ready = demo-grade:** the happy path works end to end in the pipeline (real or fake neighbors), there is at least one pytest smoke test, the README lists
  run steps, known limits and what is mocked, CI is green, and the arm can be switched back to its fake by env flag. It does not need to handle every real-world case.
- Contracts-first: `contracts/` changes go in their own small PR before dependent arms. Cross-arm helpers go only in `utils/`.
- The pipeline stays end-to-end at every merge: `make demo` runs intent -> checkout with fakes.
- PR description: what works, what is mocked, how to demo it.
- **Fix in CLAUDE.md §7:** Isaac Sim 6.2 uses Python **3.12**, not 3.11. 3.11 applies only to Isaac 5.x. Keep code 3.11-compatible anyway.

---

## 12. Open questions (owner + deadline)

| Question | How to resolve | By |
|---|---|---|
| ~~Does anyone have an RTX 4080+ box?~~ Resolved: the Isaac worker runs on "vhagar" | – | Done |
| Which Isaac version, GPU and $/hr does Brev give? (backup only now) | Trial deploy, only if vhagar fails | If needed |
| DubHacks rules: team size, pre-existing code, Devpost URL, number of sponsor tracks, judging format | Hacker Guide in a browser, organizers | Hour 1 |
| Visa track text and rubric; does Visa hand out sandbox keys or require a Visa API? | Visa table / Discord | Hour 2 |
| Confirm the T-Mobile brief docx wording and rubric (30 / 25 / 20 / 25 per the team plan); SideShift track text; prizes | Brief docx, sponsor tables | Hour 2 |
| Sponsor-track limit (prior year: two sponsored tracks + title); we list three side tracks. OPEN TEAM DECISION; default cut order Visa → T-Mobile → Photon → SideShift | Organizers, then team | Sat evening |
| Live demo store: grocery `store_ave` or wireless `store_tmobile` (OPEN TEAM DECISION) | Team | Sat ~9–10 PM |
| Photon: beta signup gating, which dashboard issues spectrum-ts creds, shared-line first-message behavior | Signup + Terminal echo + Discord | Hour 1 |
| Anthropic Evaluation-tier limits; any credits | Console limits page, Anthropic table | Hour 1 |
| `penalty_m` scale K (interest -> meters) | routing + recommender owners agree | Hour 4 |
| Store size and aisle width in the planogram (grid resolution, inflation) | catalog owner | Hour 2 |

---

## 13. What the fact-check changed, and the unverified register

**Corrected (wrong -> right):**
1. "The Photon Python client cannot send or receive messages" is **wrong**. `photonhq-api` 0.4.2 (2026-10-15) added `sendMessage` /
   `listMessages` (`send_message` / `list_messages` in Python), and 0.5.0 (2026-10-16) is the latest (its breaking changes are Go only, and it needs Python 3.11+).
   A Python-only messaging arm is viable, so **the Node sidecar is optional**. The agent-stack advice "never call Photon from Python" is withdrawn.
2. "The Cybersource SDK needs Python 3.6+" is **wrong**. The latest is 0.0.80 (2026-10-05) with no `requires_python` and classifiers for 2.7 / 3.5-3.8. Test it on 3.12.
3. "Isaac omap defaults 4/5/6" is **wrong**. The defaults are cell size 0.05, occupied 1.0, unoccupied 0.0, unknown 0.5.
4. The import path `isaacsim.robot.wheeled_robots.controllers.differential_controller` is **deprecated in 6.x**. Python controllers moved to
   `isaacsim.robot.experimental.wheeled_robots`, and nodes are in `isaacsim.robot.wheeled_robots.nodes`. Use pure-Python pure pursuit with set-pose.
5. Minor: Opus 5.5's default effort is **medium** (not high). Fable cache reads cost 2.5%. The Kroger third-party samples **do** show `bayNumber`.
   "Photon beta requires request-access approval" (agent-stack and hackathon reports) is not supported by the beta docs (⚠️ still unverified). The OR-Tools
   "penalty -1 = mandatory" claim is not in the docs, so drop it. `CLAUDE.md`'s "Isaac Python 3.11" holds only for 5.x.

**⚠️ Unverified register (check before relying on it):**
- DubHacks: rubric weights 25/25/20/15/15; the Visa track name (from an image filename); the T-Mobile/Photon/SideShift track text; team size,
  pre-existing code, Devpost, judging format; the "two sponsored tracks" rule (prior year).
- Photon: beta free with no waitlist; $100 credits; free plan of 25 shared assignments; prizes and judging criteria applying to DubHacks; spectrum-ts
  credential source; stable-docs first-message behavior; 5 req/s HTTP limit; Python method signatures (copy from the guide).
- Visa: VIC sandbox path and lead time; TAP uses Ed25519; TAP licence terms; AP2 Intent/Cart/Payment naming; 4111 test PAN in the Cybersource sandbox;
  TAP "GA June 2026".
- Stripe: "raw PAN blocked unless enabled"; exact PaymentIntent params (standard, not re-fetched).
- Isaac: Brev build time, version, GPU and price; AWS AMI details; GPU cloud prices; Isaac 6.0 GA date; `/Isaac/Robots` prefix; no Fetch/TIAGo;
  surface-gripper open call; "CleanRoom3D" does not exist (negative search only).
- Perception/data: OpenCV 5 breaks YuNet 2023mar; SigLIP 2 dependency minimums and MPS speed; OFF dump URLs, schema and licences; all Kroger details.
- Embeddings/DB: Voyage free tier and pricing; the Supabase PostgREST change on Oct 30.
- Routing: solve-time and MCP timings; scikit-image / networkx pins; whether ortools has a release newer than 9.15.
- Anthropic: Evaluation-tier limits; hackathon credits.
- Voice: Deepgram/Cartesia latency.
- Pitch: every stat in §8, the Caper and Simbe figures, and the OpenAI Instant Checkout status.
