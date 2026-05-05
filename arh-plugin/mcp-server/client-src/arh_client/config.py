import os

from pydantic import BaseModel


class Config(BaseModel):
    api_base_url: str = "https://api.airesearcherhub.com"
    api_key: str = ""
    agent_handle: str = ""
    disable_tracking: bool = False
    log_locally: bool = True
    local_log_dir: str = os.path.expanduser("~/.arh/logs")


_config = Config()


def configure(
    api_key: str = "",
    api_base_url: str = "",
    agent_handle: str = "",
    **kwargs,
) -> Config:
    """Configure the ARH client globally.

    Args:
        api_key: API key for authentication (arh_sk_... format)
        api_base_url: Base URL of the ARH API
        agent_handle: Agent handle for identification
        **kwargs: Additional config fields (disable_tracking, log_locally, local_log_dir)

    Returns:
        Updated Config object
    """
    global _config
    updates = {}
    if api_key:
        updates["api_key"] = api_key
    if api_base_url:
        updates["api_base_url"] = api_base_url
    if agent_handle:
        updates["agent_handle"] = agent_handle
    updates.update(kwargs)
    _config = _config.model_copy(update=updates)
    return _config


def get_config() -> Config:
    """Get the current global configuration."""
    return _config
