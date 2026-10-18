"""contracts 0.2.2: joint-level SO-101 control on the sim wire (ArmJointsCmd + ArmJointState / PolicyFrame results)."""

import pytest

import contracts
from contracts.sim import (
    ARM_JOINTS_CAPABILITY,
    ARM_JOINTS_MAX_STEPS,
    GATEWAY_TO_SIM_ADAPTER,
    MOTION_COMMANDS,
    SIM_COMMAND_ADAPTER,
    ArmJointsCmd,
    CommandResult,
    command_error_result,
    default_timeout_s,
    parse_gateway_to_sim,
    parse_sim_message,
    versions_compatible,
)
from contracts.skill_server import SO101_JOINT_NAMES


def test_version_is_a_patch_so_0_2_workers_still_connect():
    assert contracts.CONTRACTS_VERSION == "0.2.2"
    assert versions_compatible("0.2.1", contracts.CONTRACTS_VERSION)
    assert versions_compatible("0.2.0", contracts.CONTRACTS_VERSION)


def test_arm_joints_is_a_motion_command_in_both_unions():
    raw = {"type": "arm_joints", "cmd_id": "c", "robot_id": "r", "targets": [[1, 2, 3, 4, 5, 6]], "lift_height_m": 0.7}
    for adapter in (SIM_COMMAND_ADAPTER, GATEWAY_TO_SIM_ADAPTER):
        assert isinstance(adapter.validate_python(raw), ArmJointsCmd)
    cmd = parse_gateway_to_sim(raw)
    assert cmd.capture == ["side", "wrist"] and cmd.hz == 30.0 and not cmd.end and not cmd.capture_each_step
    assert "arm_joints" in MOTION_COMMANDS and default_timeout_s(cmd) == 30.0
    assert ARM_JOINTS_CAPABILITY == "arm_joints" and len(SO101_JOINT_NAMES) == 6


@pytest.mark.parametrize(
    "targets",
    [[[0, 0, 0]], [[0, 0, 0, 0, 0, float("nan")]], [[0] * 6] * (ARM_JOINTS_MAX_STEPS + 1)],
)
def test_bad_rows_are_rejected_and_answered(targets):
    raw = {"type": "arm_joints", "cmd_id": "c", "robot_id": "r", "targets": targets}
    with pytest.raises(ValueError):
        parse_gateway_to_sim(raw)
    res = command_error_result(raw, ValueError("bad"))
    assert res is not None and not res.ok and res.error_code == "internal"


def test_results_carry_arm_state_and_policy_frames_and_old_parsers_ignore_them():
    res = CommandResult(
        cmd_id="c",
        robot_id="r",
        ok=True,
        instance_id="s#00",
        arm={
            "q_deg": [0, -100, 90, 50, 45, -10],
            "joint_mode": True,
            "lift_height_m": 0.62,
            "gripper_closed": True,
            "attached": "s#00",
            "steps_applied": 25,
            "events": ["sticky_grasp:s#00"],
        },
        policy_frames=[{"camera": "side", "step": 25, "jpeg_b64": "AA==", "w": 640, "h": 480}],
    )
    back = parse_sim_message(res.model_dump_json())
    assert back.arm.attached == "s#00" and back.arm.joint_names == list(SO101_JOINT_NAMES)
    assert back.policy_frames[0].camera == "side" and not back.policy_frames[0].blurred
    # a nested unknown key from a newer worker is dropped, not fatal
    data = res.model_dump(mode="json")
    data["arm"]["future_field"] = 1
    assert parse_sim_message(data).arm.steps_applied == 25
