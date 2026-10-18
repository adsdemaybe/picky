# Picky UI direction

Built with shadcn/ui (radix, Tailwind 4) and Lucide icons. Grounded in ui-ux-pro-max's profiles for **Grocery & Shopping**
(flat, vibrant, block-based; fresh green + food-category colors; micro-interactions) and **Personal Finance** (calm
blue + success green + alert red), with the Frontend Design rule: no generic AI-default look.

## Brief
Who: someone doing their weekly shop and keeping an eye on money. Job: "is this a good price, where's it cheapest,
can I afford it, and can the robot just go get it?" Feel: a market you enjoy walking through, not a bank dashboard.

## Palette (5 named colors + neutrals)
| Name | Hex | Used for |
|---|---|---|
| Leaf | `#0F7A4A` | brand, primary actions, in-store, "good price" |
| Lime | `#D9F26B` | highlights you should notice: savings, deals, selection |
| Sky | `#2F5BEA` | money and info: budget, spending, the assistant |
| Amber | `#F2A516` | wait / heads-up |
| Tomato | `#D93A30` | over budget, skip, errors |
| Neutrals | `#F3F5F2` page, `#FFFFFF` cards, `#111111` text, `#586160` secondary text | |
Products get their own color from the catalog (label colors) so lists look like shelves, not spreadsheets.

## Type
- **Bricolage Grotesque** for headings and big prices: friendly, a little quirky, very legible at size.
- **Plus Jakarta Sans** for everything else, tabular figures for money.
- Sentence-case labels. No monospace labels, no all-caps eyebrows, no "·"-joined meta strings, no "→" on links.

## Layout
```
Desktop                                              Mobile
┌──────────────────────────────────────────────┐     ┌──────────────┐
│ picky  [ Search a product or ask anything  ] │     │ picky   (M)  │
│        Home  Find  Spending  Robot    (M)    │     │ [search/ask] │
├──────────────────────────────────────────────┤     │              │
│ page content, 1200px max, 12-col grid        │     │  content     │
│ hero = the thing you came for on that page   │     │              │
│ (search on Find, the camera on Robot)        │     ├──────────────┤
└──────────────────────────────────────────────┘     │ ⌂  ⌕  ◔  ▣  ✦│ bottom tabs
                                                     └──────────────┘
```
- The top search box is the one entry point: product names go to Find, questions open Ask Picky.
- Robot = order + live trip in one place.
- Cards are white on the light page with a hairline ring; color blocks (product tiles, verdict chips) carry meaning.

## Motion and media
- Motion (`motion/react`, formerly framer-motion) with `MotionConfig reducedMotion="user"`: page enter transition
  (`app/template.tsx`), sliding nav pill, staggered lists, money that counts up, price lines that draw themselves,
  springy approval cards, basket items sliding in, confetti on payment.
- Lottie and image slots (`src/lib/media.ts`, docs in `public/MEDIA.md`): drop `public/lottie/*.lottie` or
  `public/images/*.jpg` and they replace the built-in animated SVG art (robot, shelves) automatically.

## Principles
1. Every color means something (store, verdict, budget state). Decoration stays neutral.
2. Show the product. Tiles with the product's color and a category icon beat text rows.
3. One motion moment per screen (tiles rise in on load; a check pops when the robot verifies an item). Reduced motion turns it off.
4. Say the answer first: "Wait until Friday, it's $2 off" before any table.
5. 44px targets, visible focus, 4.5:1 text contrast, works at 375px.
