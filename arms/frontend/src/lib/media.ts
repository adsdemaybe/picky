// Media slots: drop a file at `path` (under arms/frontend/public) and it replaces the built-in animated fallback.
// No code change needed. Keep Lottie files small (< 300 KB); .lottie (dotLottie) or .json both work.
export type MediaSlot = { path: string; kind: "lottie" | "image"; where: string; suggest: string };

export const MEDIA = {
  robotHero: {
    path: "/lottie/robot-hero.lottie",
    kind: "lottie",
    where: "Home hero, right side",
    suggest: "LottieFiles search: 'shopping robot', 'delivery robot', 'robot cart'. Looping, ~1:1, light background.",
  },
  robotIdle: {
    path: "/lottie/robot-idle.lottie",
    kind: "lottie",
    where: "Live trip page when no trip is running",
    suggest: "LottieFiles search: 'robot sleeping', 'robot waiting'. Looping, ~1:1.",
  },
  celebrate: {
    path: "/lottie/celebrate.lottie",
    kind: "lottie",
    where: "Receipt after checkout (plays once)",
    suggest: "LottieFiles search: 'confetti', 'success check'. Non-looping, transparent background.",
  },
  thinking: {
    path: "/lottie/thinking.lottie",
    kind: "lottie",
    where: "Ask Picky while it's answering",
    suggest: "LottieFiles search: 'typing dots', 'ai thinking'. Looping, tiny (48px tall).",
  },
  storeGrocery: {
    path: "/images/store-grocery.jpg",
    kind: "image",
    where: "Robot order page, Picky Market card thumbnail (square crop)",
    suggest: "Bright grocery aisle or produce display, square ~600x600, no faces.",
  },
  storeWireless: {
    path: "/images/store-wireless.jpg",
    kind: "image",
    where: "Robot order page, T-Mobile card thumbnail (square crop)",
    suggest: "Phone store display table or accessory wall, square ~600x600.",
  },
  robotPhoto: {
    path: "/images/robot.jpg",
    kind: "image",
    where: "Home, 'How Picky works' step 3",
    suggest: "A photo or render of your robot (Isaac Sim screenshot works), ~800x800.",
  },
} satisfies Record<string, MediaSlot>;

export type MediaKey = keyof typeof MEDIA;
