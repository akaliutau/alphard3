from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from utilities.settings import TEMPLATES_DIR


class PromptManager:
    def __init__(self, template_folder: Path = TEMPLATES_DIR):
        if not template_folder.is_dir():
            raise FileNotFoundError(f"Template folder not found: {template_folder}")
        self.env = Environment(
            loader=FileSystemLoader(template_folder),
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )

    def compose_prompt(self, template_name: str, **kwargs: Any) -> str:
        return self.env.get_template(template_name).render(**kwargs)
