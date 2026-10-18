# Picky sim assets: licenses and attribution

This notice travels with the asset tree on the GPU host (`picky-assets/products/` and
`picky-assets/robots/picky-rover/`). The source of truth is `arms/sim/ASSETS.md` in the Picky repo.

## Product 3D models: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)

1. NVIDIA SimReady Assets, (c) NVIDIA, CC BY 4.0. https://huggingface.co/datasets/nvidia/simready-assets
   Each package keeps its own license at `simready/<id>/.metadata/LICENSE.txt`. Ids start with `sr_` in `index.yaml`.
2. RoboCasa365 object library, AI-generated objects. Nasiriany et al., "RoboCasa: Large-Scale Simulation of Everyday
   Tasks for Generalist Robots", RSS 2024. https://robocasa.ai. CC BY 4.0 per the RoboCasa README
   (https://github.com/robocasa/robocasa#license); the zip has no license file. Ids start with `rc_`.
3. Lightwheel kitchen objects, via NVIDIA "PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF", CC BY 4.0 (dataset
   card). https://huggingface.co/datasets/nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF. Ids start with
   `lw_`.

Per asset, `index.yaml` records `source`, `source_id`, `source_usd`, `license` and `attribution`. The models Picky
draws are the `asset_id` / `parts[].asset` values in the repo's `data/sim/sku_assets.yaml`.

Changes made by the Picky team: normalized to Z-up, meters, origin at the bottom center, upright with the front toward
-Y; SDF colliders swapped for convex hulls and the rigid body moved to the unit root; MJCF/OBJ converted to USD;
rescaled per SKU; our own label art laid over the front.

## Robot model: Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0)

- `lekiwi.usd` from LeIsaac (LightwheelAI), https://huggingface.co/LightwheelAI/leisaac_env, unmodified.
- Robot designs: LeKiwi (SIGRobotics-UIUC, https://github.com/SIGRobotics-UIUC/LeKiwi) and SO-101 / SO-ARM100
  (TheRobotStudio, https://github.com/TheRobotStudio/SO-ARM100).
- `picky_rover.usda` is the Picky team's overlay (lift, mast, basket, cameras). It sublayers `lekiwi.usd` unchanged.

## Credit line

Product 3D models: NVIDIA SimReady Assets (CC BY 4.0), RoboCasa (Nasiriany et al., RSS 2024; CC BY 4.0),
Lightwheel/NVIDIA PhysicalAI Kitchen objects (CC BY 4.0); modified by the Picky team. Robot model: LeIsaac LeKiwi +
SO-101 (LightwheelAI, Apache-2.0; LeKiwi by SIGRobotics-UIUC, SO-101 by TheRobotStudio), with a lift overlay by the
Picky team.

## Trademark caution

CC BY covers copyright, not trademarks. Some SimReady label art resembles real brands (`brand_risk` in `index.yaml`:
Dr. Oetker, Nesquik, Coca-Cola contour bottle, Heinz-like, Lay's-like "Lysa", Doritos-like "Doradoss", Del Monte-like
"Monte Delight"). Picky's map uses only the three "Monte Delight" bean cans, and our 360-degree label wrap covers them.
