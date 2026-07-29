"""Fixture: handler that drifts from the schema in robot_status.proto."""


def build_status_legacy(state):
    # hand-rolled-contract: this dict speaks the RobotStatus schema;
    # toolName is contract-case-skew (schema declares tool_name);
    # plannerVersion is contract-drift-key (no planner_version anywhere)
    return {
        "parent_frame": state.frame,
        "joint_speed_limit": state.speed_limit,
        "error_code": state.error,
        "toolName": state.tool,
        "is_homed": state.homed,
        "plannerVersion": state.planner,
    }
