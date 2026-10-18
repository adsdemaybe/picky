"""Build the Picky robot: DISCOVERSE rm2_car (4-wheel swerve base + AIRBOT Play arm) with a basket on the deck.

DISCOVERSE (MIT, github.com/discoverse-dev/DISCOVERSE) ships the rm2_car kinematics and the AIRBOT Play arm meshes,
but NOT the chassis/wheel/steering meshes. This script keeps the real kinematics + arm meshes, replaces the missing
chassis meshes with primitives, welds a basket onto the rear of the deck (geoms on the base body = rigidly fixed,
the most stable option in PhysX), converts OBJ -> STL (Isaac Sim on DGX Spark cannot import OBJ), and writes one
self-contained MJCF ready for Isaac Sim's MJCF importer.

    cd arms/sim/supermarket
    python robot/build_rm2_basket.py --discoverse ~/assets/DISCOVERSE --out assets/build/rm2_basket
    python robot/build_rm2_basket.py ... --check   # also load in MuJoCo, settle, render a preview PNG
"""

from __future__ import annotations

import argparse
import copy
import xml.etree.ElementTree as ET
from pathlib import Path

ARM_MESHES = [
    "arm_base_0", "arm_base_1", "link1", "link2_0", "link2_1", "link3_0", "link3_1",
    "link4", "link5_0", "link5_1", "link6", "left", "right",
]  # fmt: skip

# Basket in the rm2 base frame (x forward, z up). The arm is mounted front-right at (0.27, -0.17), so the basket sits
# on the rear of the top deck, inside the arm's reach (~0.45 m from the shoulder).
DECK_TOP_Z = 0.345
BASKET_CENTER = (-0.13, 0.04)
BASKET_INNER = (0.34, 0.30)  # x, y inner size (m)
BASKET_HEIGHT = 0.16
BASKET_WALL = 0.008

HEADER = """<mujoco model="picky_rm2_basket">
  <compiler angle="radian" meshdir="meshes" autolimits="true"/>
  <option integrator="implicitfast" timestep="0.002"/>
  <default>
    <default class="obj_visual"><geom type="mesh" contype="0" conaffinity="0" group="2"/></default>
    <default class="joint1"><joint axis="0 0 1" range="-3.151 2.089" damping="0.2"/>
      <position kp="1000" ctrlrange="-3.151 2.089"/></default>
    <default class="joint2"><joint axis="0 0 1" range="-2.963 0.181" damping="0.2"/>
      <position kp="1000" ctrlrange="-2.963 0.181"/></default>
    <default class="joint3"><joint axis="0 0 1" range="-0.094 3.161" damping="0.2"/>
      <position kp="1000" ctrlrange="-0.094 3.161"/></default>
    <default class="joint4"><joint axis="0 0 1" range="-3.012 3.012" damping="0.1"/>
      <position kp="350" ctrlrange="-3.012 3.012"/></default>
    <default class="joint5"><joint axis="0 0 1" range="-1.859 1.859" damping="0.1"/>
      <position kp="350" ctrlrange="-1.859 1.859"/></default>
    <default class="joint6"><joint axis="0 0 1" range="-3.017 3.017" damping="0.1"/>
      <position kp="350" ctrlrange="-3.017 3.017"/></default>
    <default class="finger1"><joint axis="0 1 0" range="-0.04 0" type="slide" damping="1"/>
      <position kp="500" ctrlrange="-0.04 0"/></default>
    <default class="finger2"><joint axis="0 1 0" range="0 0.04" type="slide" damping="1"/>
      <position kp="500" ctrlrange="0 0.04"/></default>
    <default class="steer_joint"><joint axis="0 0 1" range="-1.5708 1.5708" damping="0.5"/>
      <position kp="50" ctrlrange="-1.5708 1.5708"/></default>
    <default class="wheel_joint"><joint axis="0 0 -1" damping="0.3"/>
      <velocity kv="20" ctrlrange="-30 30"/></default>
  </default>
  <asset>
    <material name="Paint_Matte_Black" rgba="0.02 0.02 0.02 1"/>
    <material name="Paint_Metallic_White" rgba="0.93 0.93 0.93 1"/>
    <material name="chassis" rgba="0.15 0.17 0.2 1"/>
    <material name="deck" rgba="0.75 0.77 0.8 1"/>
    <material name="tire" rgba="0.08 0.08 0.08 1"/>
    <material name="basket" rgba="0.1 0.45 0.85 1"/>
{meshes}
  </asset>
  <worldbody>
    <body name="picky" pos="0 0 0">
      <freejoint name="root"/>
{rm2}
    </body>
  </worldbody>
  <actuator>
{actuators}
  </actuator>
  <equality>
    <joint joint1="finger_left_joint" joint2="finger_right_joint" polycoef="0 -1 0 0 0"/>
  </equality>
</mujoco>
"""


def convert_meshes(discoverse: Path, out_meshes: Path) -> None:
    import trimesh

    out_meshes.mkdir(parents=True, exist_ok=True)
    src = discoverse / "models" / "meshes" / "airbot_play"
    for name in ARM_MESHES:
        obj = src / f"{name}.obj"
        if obj.stat().st_size < 1024 and obj.read_text(errors="ignore").startswith("version https://git-lfs"):
            raise SystemExit(f"{obj} is a Git LFS pointer; run `git lfs pull` in the DISCOVERSE checkout")
        mesh = trimesh.load(obj, force="mesh")
        mesh.export(out_meshes / f"{name}.stl")


def arm_body(discoverse: Path) -> ET.Element:
    """The AIRBOT Play arm subtree from DISCOVERSE, minus the camera mount (massless bodies upset PhysX)."""
    xml = discoverse / "models" / "mjcf" / "mobile_chassis" / "rm2_car" / "arm_gripper.xml"
    root = ET.parse(xml).getroot()
    arm = copy.deepcopy(root.find("body"))
    for parent in arm.iter("body"):
        for child in list(parent):
            if child.tag == "body" and child.get("name") == "camera":
                parent.remove(child)
    return arm


def box(name: str, pos, size, material: str, collide: bool = True) -> str:
    c = "" if collide else ' contype="0" conaffinity="0"'
    return (
        f'<geom name="{name}" type="box" pos="{pos[0]:.4f} {pos[1]:.4f} {pos[2]:.4f}" '
        f'size="{size[0]:.4f} {size[1]:.4f} {size[2]:.4f}" material="{material}"{c}/>'
    )


def basket_geoms() -> list[str]:
    cx, cy = BASKET_CENTER
    ix, iy = BASKET_INNER
    w, h = BASKET_WALL, BASKET_HEIGHT
    z0 = DECK_TOP_Z
    zc = z0 + w + h / 2
    return [
        box("basket_floor", (cx, cy, z0 + w / 2), (ix / 2 + w, iy / 2 + w, w / 2), "basket"),
        box("basket_wall_front", (cx + ix / 2 + w / 2, cy, zc), (w / 2, iy / 2 + w, h / 2), "basket"),
        box("basket_wall_back", (cx - ix / 2 - w / 2, cy, zc), (w / 2, iy / 2 + w, h / 2), "basket"),
        box("basket_wall_left", (cx, cy + iy / 2 + w / 2, zc), (ix / 2, w / 2, h / 2), "basket"),
        box("basket_wall_right", (cx, cy - iy / 2 - w / 2, zc), (ix / 2, w / 2, h / 2), "basket"),
    ]


# (name, pos in rm2 frame, wheel y-offset inside the steering body) — from DISCOVERSE rm2_car.xml.
STEERING = [
    ("1", (0.2585, 0.184), -0.02),
    ("2", (0.2585, -0.184), 0.02),
    ("3", (-0.238, -0.184), 0.02),
    ("4", (-0.238, 0.184), -0.02),
]


def rm2_xml(arm: ET.Element) -> str:
    lines = [
        '<body name="rm2" pos="0 0 0.51">',
        '  <inertial pos="-0.05 0 -0.2" mass="30" diaginertia="1.2 1.6 2.0"/>',
        "  " + box("chassis", (0.01, 0, -0.2), (0.3, 0.13, 0.08), "chassis"),
    ]
    for i, (sx, sy) in enumerate([(0.29, 0.19), (0.29, -0.19), (-0.29, 0.19), (-0.29, -0.19)]):
        lines.append("  " + box(f"post{i}", (sx, sy, 0.06), (0.02, 0.02, 0.28), "chassis"))
    lines.append("  " + box("deck", (0, 0, DECK_TOP_Z - 0.01), (0.33, 0.22, 0.01), "deck"))
    lines += ["  " + g for g in basket_geoms()]
    for n, (x, y), wy in STEERING:
        lines += [
            f'  <body name="steering{n}" pos="{x} {y} -0.228">',
            '    <inertial pos="0 0 -0.05" mass="0.37" diaginertia="0.0009 0.0007 0.0005"/>',
            f'    <joint name="steer_joint{n}" class="steer_joint"/>',
            "    " + box(f"steer_housing{n}", (0, 0, -0.06), (0.04, 0.05, 0.06), "chassis", collide=False),
            f'    <body name="wheel{n}" pos="0 {wy} -0.185" quat="0.707105 0.707108 0 0">',
            '      <inertial pos="0 0 0" mass="1.2" diaginertia="0.003 0.003 0.006"/>',
            f'      <joint name="wheel_joint{n}" class="wheel_joint"/>',
            f'      <geom name="tire{n}" type="cylinder" size="0.1 0.035" material="tire" friction="1.2 0.01 0.001"/>',
            "    </body>",
            "  </body>",
        ]
    lines += [
        '  <body name="arm_base" pos="0.27 -0.17 0.345">',
        '    <inertial pos="0 0 0.05" mass="0.8" diaginertia="0.002 0.002 0.002"/>',
        '    <geom mesh="arm_base_0" material="Paint_Metallic_White" class="obj_visual"/>',
        '    <geom mesh="arm_base_1" material="Paint_Matte_Black" class="obj_visual"/>',
        '    <geom name="arm_base_col" type="cylinder" size="0.05 0.055" pos="0 0 0.055" rgba="0 0 0 0"/>',
    ]
    ET.indent(arm, space="  ", level=2)
    lines.append("    " + ET.tostring(arm, encoding="unicode").strip())
    lines += ["  </body>", "</body>"]
    return "\n".join("      " + ln for ln in lines)


def actuators() -> str:
    acts = [f'<position name="steer{n}" joint="steer_joint{n}" class="steer_joint"/>' for n, _, _ in STEERING]
    acts += [f'<velocity name="wheel{n}" joint="wheel_joint{n}" class="wheel_joint"/>' for n, _, _ in STEERING]
    acts += [f'<position name="arm_joint{i}" joint="arm_joint{i}" class="joint{i}"/>' for i in range(1, 6)]
    acts += [
        '<position name="arm_joint6" joint="gripper_base_joint" class="joint6"/>',
        '<position name="gripper" joint="finger_right_joint" class="finger2"/>',
    ]
    return "\n".join("    " + a for a in acts)


def check(xml_path: Path) -> None:
    import mujoco
    import numpy as np

    m = mujoco.MjModel.from_xml_path(str(xml_path))
    d = mujoco.MjData(m)
    mujoco.mj_resetData(m, d)
    d.qpos[2] = 0.02
    for _ in range(1500):  # 3 s: drop onto a ground plane equivalent (z clamp) and settle
        mujoco.mj_step(m, d)
    print(f"bodies={m.nbody} joints={m.njnt} actuators={m.nu} total_mass={m.body_subtreemass[1]:.1f} kg")
    print(f"root z after settle (no floor in model, free-falls): {d.qpos[2]:.3f}")
    finite = bool(np.isfinite(d.qpos).all())
    print(f"simulation stable: {finite}")

    spec_xml = (
        xml_path.read_text()
        .replace("<asset>", '<visual><global offwidth="960" offheight="720"/></visual>\n  <asset>', 1)
        .replace(
            "<worldbody>",
            '<worldbody>\n    <light pos="1 -1 3" dir="-0.3 0.3 -1"/>\n'
            '    <geom name="floor" type="plane" size="5 5 0.1" rgba="0.8 0.8 0.8 1"/>',
        )
    )
    preview = xml_path.with_name("_preview.xml")
    preview.write_text(spec_xml)
    m2 = mujoco.MjModel.from_xml_path(str(preview))
    d2 = mujoco.MjData(m2)
    d2.qpos[2] = 0.0
    home = {"arm_joint2": -0.6, "arm_joint3": 1.4}
    for i in range(m2.nu):
        name = mujoco.mj_id2name(m2, mujoco.mjtObj.mjOBJ_ACTUATOR, i)
        d2.ctrl[i] = home.get(name, 0.0)
    for _ in range(1500):
        mujoco.mj_step(m2, d2)
    print(f"on floor: root z={d2.qpos[2]:.3f} (expect ~0), tilt quat={np.round(d2.qpos[3:7], 3)}")
    r = mujoco.Renderer(m2, 720, 960)
    cam = mujoco.MjvCamera()
    cam.lookat[:] = [0, 0, 0.75]
    cam.distance, cam.azimuth, cam.elevation = 3.0, 135, -22
    r.update_scene(d2, cam)
    from PIL import Image

    Image.fromarray(r.render()).save(xml_path.with_name("preview.png"))
    preview.unlink()
    print(f"preview: {xml_path.with_name('preview.png')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discoverse", type=Path, required=True, help="DISCOVERSE checkout (with git lfs pull done)")
    ap.add_argument("--out", type=Path, default=Path("arms/sim/supermarket/assets/build/rm2_basket"))
    ap.add_argument("--check", action="store_true", help="load in MuJoCo and render preview.png")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    convert_meshes(args.discoverse, args.out / "meshes")
    meshes = "\n".join(f'    <mesh name="{n}" file="{n}.stl"/>' for n in ARM_MESHES)
    xml = HEADER.format(meshes=meshes, rm2=rm2_xml(arm_body(args.discoverse)), actuators=actuators())
    xml_path = args.out / "rm2_basket.xml"
    xml_path.write_text(xml)
    print(f"wrote {xml_path}")
    if args.check:
        check(xml_path)


if __name__ == "__main__":
    main()
