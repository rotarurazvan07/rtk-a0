"""Slash-command handler: opens the plugin's savings page.

The command itself carries no logic. Every data call in this surface happens
only when the user opens the page, keeping the zero-background-load contract.
"""

from pathlib import Path


def _page_route() -> str:
    """Route of the savings page relative to the webui root.

    The installed directory name is derived from this handler's own file
    location, so one shipped copy serves every install shape: the dev checkout
    directory, the manifest directory, or any Hub-installed directory name.
    """
    installed_name = Path(__file__).resolve().parents[1].name
    return f"plugins/{installed_name}/webui/main.html"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the single open_modal effect the command palette executes."""
    return {"text": "", "effects": [{"type": "open_modal", "path": "/" + _page_route()}]}
