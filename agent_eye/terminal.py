"""Small, dependency-free terminal styling helpers."""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import TextIO


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"


def supports_color(stream: TextIO) -> bool:
    """Use color only when it is useful and explicitly permitted."""
    return (
        bool(getattr(stream, "isatty", lambda: False)())
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "") != "dumb"
    )


def paint(text: str, *styles: str, stream: TextIO | None = None) -> str:
    output = sys.stdout if stream is None else stream
    if not supports_color(output):
        return text
    return f"{''.join(styles)}{text}{RESET}"


class ColorHelpFormatter(argparse.HelpFormatter):
    """Apply color after argparse has calculated plain-text alignment."""

    def format_help(self) -> str:
        rendered = super().format_help()
        if not supports_color(sys.stdout):
            return rendered

        rendered = re.sub(
            r"(?m)^(usage:)",
            f"{BOLD}{CYAN}\\1{RESET}",
            rendered,
        )
        rendered = re.sub(
            r"(?m)^([^ \n][^\n]*:)$",
            f"{BOLD}{YELLOW}\\1{RESET}",
            rendered,
        )
        rendered = re.sub(
            r"(?<![\w])(--?[a-zA-Z][a-zA-Z0-9_-]*)",
            f"{GREEN}\\1{RESET}",
            rendered,
        )
        return rendered
