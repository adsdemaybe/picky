# Drop-in media for the Picky app

Put a file at one of these paths and the app uses it automatically, with no code change.
Until then each slot shows a built-in animated illustration (and, in `pnpm dev` only, a small path hint).
Slot list lives in `src/lib/media.ts`.

| File | Where it shows | What to look for |
|---|---|---|
| `lottie/robot-hero.lottie` | Home hero | LottieFiles: "shopping robot" / "delivery robot", looping, ~1:1 |
| `lottie/robot-idle.lottie` | Live trip when no trip is running | "robot sleeping" / "robot waiting", looping |
| `lottie/celebrate.lottie` | Receipt after checkout (plays once) | "confetti" / "success check", transparent |
| `lottie/thinking.lottie` | Ask Picky while answering | "typing dots" / "ai thinking", tiny |
| `images/store-grocery.jpg` | Robot order, Picky Market card thumbnail | Grocery aisle or produce, square ~600x600, no faces |
| `images/store-wireless.jpg` | Robot order, T-Mobile card thumbnail | Phone display table, square ~600x600 |
| `images/robot.jpg` | (reserved) How Picky works | Photo or Isaac Sim render of the robot, ~800x800 |

`.json` Lottie files work too: rename the path in `src/lib/media.ts`. Check each file's license on LottieFiles
(the free "Lottie Simple License" allows use in apps). Keep Lottie files under ~300 KB.
