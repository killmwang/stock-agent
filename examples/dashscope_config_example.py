#!/usr/bin/env python3
"""Example: configure Stock Agent to use DashScope-compatible models."""

import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from stock_agent.default_config import DEFAULT_CONFIG


def create_dashscope_config():
    """Create a runtime config for DashScope models."""
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": "dashscope",
            "backend_url": "https://dashscope.aliyuncs.com/api/v1",
            "deep_think_llm": "qwen-plus",
            "quick_think_llm": "qwen-turbo",
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "online_tools": True,
        }
    )
    return config


def check_dashscope_setup():
    """Verify local DashScope configuration without printing secrets."""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("DASHSCOPE_API_KEY is not set.")
        return False

    try:
        import dashscope  # noqa: F401
    except ImportError:
        print("dashscope package is not installed. Run: pip install dashscope")
        return False

    try:
        from stock_agent.llm_adapters.dashscope_adapter import ChatDashScope  # noqa: F401
    except ImportError as exc:
        print(f"DashScope adapter is unavailable: {exc}")
        return False

    print("DashScope configuration looks ready.")
    return True


def main():
    if not check_dashscope_setup():
        return

    config = create_dashscope_config()
    print("Provider:", config["llm_provider"])
    print("Deep model:", config["deep_think_llm"])
    print("Quick model:", config["quick_think_llm"])


if __name__ == "__main__":
    main()
