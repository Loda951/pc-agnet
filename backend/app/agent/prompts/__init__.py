from app.agent.prompts.dynamic import (
    ERROR_DEFAULT_ACTIONS,
    FAILURE_ACTION_RULES,
    build_orchestrator_input,
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
    build_tool_failure_prompt,
)
from app.agent.prompts.static import (
    AGENT_IDENTITY,
    BOUNDARY_PROTOCOL_PROMPT,
    FACT_SOURCE_POLICY,
    MEMORY_CONTEXT_POLICY,
    ORCHESTRATION_POLICY,
    ORCHESTRATOR_BASE_PROMPT,
    ROUTING_EXAMPLES,
    SYSTEM_PROMPT,
    TOOL_SELECTION_RULES,
)

ORCHESTRATOR_SYSTEM_PROMPT = ORCHESTRATOR_BASE_PROMPT

__all__ = [
    "AGENT_IDENTITY",
    "BOUNDARY_PROTOCOL_PROMPT",
    "ERROR_DEFAULT_ACTIONS",
    "FACT_SOURCE_POLICY",
    "FAILURE_ACTION_RULES",
    "MEMORY_CONTEXT_POLICY",
    "ORCHESTRATION_POLICY",
    "ORCHESTRATOR_BASE_PROMPT",
    "ORCHESTRATOR_SYSTEM_PROMPT",
    "ROUTING_EXAMPLES",
    "SYSTEM_PROMPT",
    "TOOL_SELECTION_RULES",
    "build_orchestrator_input",
    "build_orchestrator_system_prompt",
    "build_orchestrator_user_prompt",
    "build_tool_failure_prompt",
]
