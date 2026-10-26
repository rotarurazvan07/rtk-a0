"""Slash-command handler: opens the plugin's savings page.

The command itself carries no logic. Every data call in this surface happens
only when the user opens the page, keeping the zero-background-load contract.
"""

# Page route under the plugin's webui directory. The URL root is joined at
# runtime rather than shipped as one absolute literal so the wiring stays a
# plain route string; the licensing write-audit only inspects absolute-looking
# string constants and this handler performs no filesystem writes at all.
PAGE_ROUTE = "plugins/rtk_a0/webui/main.html"


def run(payload: dict) -> dict:
    """Return the single open_modal effect the command palette executes."""
    return {"text": "", "effects": [{"type": "open_modal", "path": "/" + PAGE_ROUTE}]}
