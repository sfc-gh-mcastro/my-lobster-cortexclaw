"""Channel registry — self-registering factory pattern.

Ported from NanoClaw's src/channels/registry.ts.
"""

from __future__ import annotations

from ..types import ChannelFactory

_registry: dict[str, ChannelFactory] = {}


def register_channel(name: str, factory: ChannelFactory) -> None:
    """Register a channel factory by name."""
    _registry[name] = factory


def get_channel_factory(name: str) -> ChannelFactory | None:
    """Look up a channel factory by name."""
    return _registry.get(name)


def get_registered_channel_names() -> list[str]:
    """Return all registered channel names."""
    return list(_registry.keys())
