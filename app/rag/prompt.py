"""
Prompt Loader

负责从yaml读取prompt模板
"""

from pathlib import Path
import yaml

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(
        name: str = "default"
):
    """
    加载prompt配置
    """

    file_path = (
            PROMPT_DIR /
            f"{name}.yaml"
    )

    if not file_path.exists():
        raise FileNotFoundError(
            f"Prompt not found: {file_path}"
        )

    with open(
            file_path,
            "r",
            encoding="utf-8"
    ) as f:
        config = yaml.safe_load(f)

    return config
