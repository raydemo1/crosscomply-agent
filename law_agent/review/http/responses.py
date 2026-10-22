"""Public JSON responses omit the private model tool dialogue."""

from fastapi.responses import JSONResponse


def public_payload(value):
    if isinstance(value, dict):
        return {key: public_payload(item) for key, item in value.items() if key != "tool_session"}
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    return value


class AgentJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(public_payload(content))
