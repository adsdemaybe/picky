// The robot's own touch screen (landscape ~1024x600 on the rover; also works on a phone).
// Server wrapper: params resolve inside <Suspense> (cacheComponents), the screen itself is a client component.
import { Suspense } from "react";
import RobotScreen, { ScreenSkeleton } from "./view";

export const instant = false;

/** The fleet's fixed robots (data/fixtures/robots.yaml); other ids still render at request time. */
export function generateStaticParams() {
  return [{ robotId: "rover-ave-1" }, { robotId: "rover-ave-2" }, { robotId: "rover-hill-1" }];
}

export default function Page({ params }: PageProps<"/robot/[robotId]/screen">) {
  return (
    <Suspense fallback={<ScreenSkeleton />}>
      <Resolved params={params} />
    </Suspense>
  );
}

async function Resolved({ params }: Pick<PageProps<"/robot/[robotId]/screen">, "params">) {
  const { robotId } = await params;
  return <RobotScreen robotId={decodeURIComponent(robotId)} />;
}
