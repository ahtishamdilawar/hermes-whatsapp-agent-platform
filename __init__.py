"""Hermes platform plugin: Meta WhatsApp Agent Platform (long-poll ``/agent/v1`` API)."""

# Hermes imports this directory as a package (``hermes_plugins.<name>``). Tools such as
# pytest may import the file standalone, where relative imports cannot work.
if __package__:
    from .adapter import register

    __all__ = ["register"]
