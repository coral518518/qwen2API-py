from __future__ import annotations

from backend.adapter.standard_request import StandardRequest
from backend.core.config import resolve_model
from backend.services.prompt_builder import messages_to_prompt
from backend.toolcall.normalize import build_tool_name_registry


def build_chat_standard_request(
    req_data: dict,
    *,
    default_model: str,
    surface: str,
    client_profile: str = "openclaw_openai",
) -> StandardRequest:
    requested_model = req_data.get("model", default_model)

    # 提取思考模式参数
    thinking_enabled = True
    thinking_mode = "Auto"
    thinking_format = "summary"

    # 支持模型后缀 -nothinking
    if requested_model and requested_model.endswith("-nothinking"):
        requested_model = requested_model[: -len("-nothinking")]
        thinking_enabled = False

    # 从请求体中提取参数 (兼容多种命名)
    if "think" in req_data:
        v = req_data["think"]
        thinking_enabled = (
            bool(v)
            if isinstance(v, (bool, int))
            else str(v).lower() not in ("false", "0", "off")
        )
    if "auto_think" in req_data:
        thinking_enabled = bool(req_data["auto_think"])

    if "thinking_mode" in req_data:
        thinking_mode = str(req_data["thinking_mode"])
    if "thinking_format" in req_data:
        thinking_format = str(req_data["thinking_format"])

    prompt_result = messages_to_prompt(req_data, client_profile=client_profile)
    tools = prompt_result.tools
    tool_names = [
        tool_name
        for tool_name in (tool.get("name") for tool in tools)
        if isinstance(tool_name, str) and tool_name
    ]

    return StandardRequest(
        prompt=prompt_result.prompt,
        response_model=requested_model,
        resolved_model=resolve_model(requested_model),
        surface=surface,
        client_profile=client_profile,
        stream=req_data.get("stream", False),
        tools=tools,
        tool_names=tool_names,
        tool_name_registry=build_tool_name_registry(tool_names),
        tool_enabled=prompt_result.tool_enabled,
        thinking_enabled=thinking_enabled,
        thinking_mode=thinking_mode,
        thinking_format=thinking_format,
    )
