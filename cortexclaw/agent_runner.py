"""Agent runner — bridges the orchestrator to the Cortex Code Agent SDK.

Replaces NanoClaw's container-runner.ts: instead of spinning up Docker
containers, we call the SDK's ``query()`` directly as a subprocess.
"""

from __future__ import annotations

import logging
from typing import Callable, Coroutine

from cortex_code_agent_sdk import (
    AssistantMessage,
    CortexCodeAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
    query,
)

from .config import CORTEX_CLI_PATH, CORTEX_CONNECTION, DOCKER_ENABLED, GROUPS_DIR
from .types import AgentOutput, RegisteredGroup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Permission callback
# ---------------------------------------------------------------------------


async def _auto_approve(
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
) -> PermissionResultAllow:
    """Auto-approve all tool calls (mirrors NanoClaw's --dangerously-skip-permissions)."""
    logger.debug("Auto-approved tool: %s", tool_name)
    return PermissionResultAllow()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

OnAgentOutput = Callable[[AgentOutput], Coroutine[None, None, None]]


def _should_use_docker(group: RegisteredGroup) -> bool:
    """Determine whether this group should run inside Docker."""
    if group.container_config and group.container_config.docker_enabled is not None:
        return group.container_config.docker_enabled
    return DOCKER_ENABLED


async def run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_jid: str,
    on_output: OnAgentOutput | None = None,
    resume_session_id: str | None = None,
) -> AgentOutput:
    """Run a Cortex Code agent for the given group and prompt.

    When *resume_session_id* is provided the SDK passes
    ``--resume <session_id>`` to the CLI, which explicitly resumes that
    exact session.  This is more reliable than ``--continue`` (which
    resumes the *last* session in the cwd and can pick up the wrong one
    if the directory was used by other sessions).

    Returns an :class:`AgentOutput` containing the status, result text,
    and the ``session_id`` reported by the CLI so the orchestrator can
    persist it for future continuations.
    """
    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # Prepend CLAUDE.md instructions if present
    claude_md = group_dir / "CLAUDE.md"
    full_prompt = prompt
    if claude_md.exists():
        instructions = claude_md.read_text()
        full_prompt = f"<system-instructions>\n{instructions}\n</system-instructions>\n\n{prompt}"

    # Resolve Docker vs host execution
    use_docker = _should_use_docker(group)
    if use_docker:
        from .docker_runner import create_docker_wrapper

        wrapper_path = create_docker_wrapper(group)
        effective_cli_path: str = str(wrapper_path)
        # Use host-side group_dir as cwd — the SDK validates the path exists
        # on the host. The container working directory is set via
        # `docker run -w /workspace/group` inside the wrapper script.
        effective_cwd: str = str(group_dir)
        logger.info("Using Docker isolation for group %s", group.folder)
    else:
        effective_cli_path = CORTEX_CLI_PATH
        effective_cwd = str(group_dir)

    # Docker containers are stateless — session data lives on the host's
    # Cortex CLI data dir which is not mounted into the container. Attempting
    # to --resume a host session inside the container fails with
    # "Session not found", so we skip resume in Docker mode.
    effective_resume = None if use_docker else (resume_session_id or None)

    options = CortexCodeAgentOptions(
        cwd=effective_cwd,
        connection=CORTEX_CONNECTION or None,
        can_use_tool=_auto_approve,
        cli_path=effective_cli_path,
        resume=effective_resume,
    )

    collected_text: list[str] = []
    status = "success"
    error_msg: str | None = None
    session_id: str | None = None

    try:
        async for message in query(prompt=full_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        collected_text.append(block.text)

            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                if message.subtype == "error":
                    status = "error"
                    error_msg = "Agent returned error result"

                # Emit final output
                result_text = "".join(collected_text).strip() or None
                output = AgentOutput(
                    result=result_text,
                    status=status,
                    error=error_msg,
                    session_id=session_id,
                )
                if on_output:
                    await on_output(output)

            elif isinstance(message, SystemMessage):
                # Log system messages for debugging
                if hasattr(message, "message") and message.message:
                    logger.debug("System: %s", message.message)

    except Exception as e:
        logger.error("Agent run failed for group %s: %s", group.folder, e)
        status = "error"
        error_msg = str(e)
        output = AgentOutput(result=None, status="error", error=error_msg)
        if on_output:
            await on_output(output)
        return output

    return AgentOutput(
        result="".join(collected_text).strip() or None,
        status=status,
        error=error_msg,
        session_id=session_id,
    )
