"""A name-to-adapter mapping, and the compatibility check that goes with it.

Deliberately the smallest thing that can work (round-8 brief, section 37): a
dictionary of factories, not a plugin system. There is no discovery, no entry points,
no hot reload and no marketplace -- an application that wants to use an adapter
constructs it or registers a factory for it, which is one line of code and needs no
infrastructure to explain.

What the registry *does* carry is the protocol-version check (section 36). An adapter
written against a protocol this build does not speak is refused outright rather than
run and hoped for: a protocol mismatch does not degrade gracefully, it produces
envelopes whose fields mean something subtly different, and those envelopes become
evidence. Failing at `create` puts the failure at startup where it is cheap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aer.adapter.protocol import AER_ADAPTER_PROTOCOL_VERSION, AgentAdapter
from aer.exceptions import AdapterError, AdapterProtocolError
from aer.runtime.serialization import JsonObject

__all__ = ["AdapterRegistry"]

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """Registered adapter factories, keyed by adapter name."""

    def __init__(self, *, protocol_version: str = AER_ADAPTER_PROTOCOL_VERSION) -> None:
        self._protocol_version = protocol_version
        self._factories: dict[str, Callable[[], AgentAdapter]] = {}

    # -- introspection -----------------------------------------------------

    @property
    def protocol_version(self) -> str:
        """The protocol version this registry accepts."""
        return self._protocol_version

    @property
    def names(self) -> tuple[str, ...]:
        """Registered adapter names, sorted."""
        return tuple(sorted(self._factories))

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def __len__(self) -> int:
        return len(self._factories)

    def __repr__(self) -> str:
        return f"AdapterRegistry(protocol={self._protocol_version!r}, adapters={list(self.names)})"

    # -- registration ------------------------------------------------------

    def register(
        self,
        factory: Callable[[], AgentAdapter],
        *,
        name: str | None = None,
        replace: bool = False,
    ) -> str:
        """Register a factory under an adapter name.

        A factory rather than an instance because an adapter is stateless by contract
        (section 9), so one instance could be shared -- but a fresh one per use removes
        any temptation to keep state in it, which is the property that makes the resume
        semantics work across a process restart.

        Args:
            factory: Callable returning an :class:`~aer.adapter.protocol.AgentAdapter`.
                Must be cheap and side-effect free: :meth:`describe` calls it.
            name: Registry key. Defaults to the name the adapter reports, which means
                the factory has to be called once to find out -- a fair trade for not
                having to pass the same string twice.
            replace: Allow overwriting an existing registration.

        Returns:
            The name it was registered under.

        Raises:
            AdapterError: the name is already registered and ``replace`` is false.
            AdapterProtocolError: the adapter reports an incompatible protocol version.
        """
        resolved = name or self._inspect(factory).name
        if resolved in self._factories and not replace:
            raise AdapterError(
                f"Adapter {resolved!r} is already registered; pass replace=True to override"
            )
        self._factories[resolved] = factory
        logger.debug("registered agent adapter %s", resolved)
        return resolved

    def unregister(self, name: str) -> None:
        """Forget a registration. Missing names are ignored.

        Idempotent because the caller who unregisters usually does so during teardown,
        where raising about an absent entry would only make shutdown noisier.
        """
        self._factories.pop(name, None)

    # -- use ---------------------------------------------------------------

    def create(self, name: str) -> AgentAdapter:
        """Instantiate the adapter registered under ``name``.

        Raises:
            AdapterError: no adapter is registered under that name. The message lists
                what is, because the usual cause is a typo in a config file.
            AdapterProtocolError: the adapter speaks a different protocol version.
        """
        try:
            factory = self._factories[name]
        except KeyError as exc:
            available = ", ".join(self.names) or "none"
            raise AdapterError(
                f"No agent adapter registered as {name!r}. Registered: {available}"
            ) from exc
        return self._inspect(factory)

    def describe(self) -> tuple[JsonObject, ...]:
        """One line per registered adapter, for an operator or a debug command.

        Instantiates each adapter, which is why factories have to be cheap. The payoff
        is that a misconfigured integration reports its problem here -- the protocol
        version is checked, and the declared capabilities are visible -- instead of at
        the first hook delivery.
        """
        described: list[JsonObject] = []
        for name in self.names:
            try:
                adapter = self.create(name)
            except AdapterError as exc:
                described.append({"name": name, "error": str(exc)})
                continue
            capabilities = adapter.capabilities()
            described.append(
                {
                    "name": name,
                    "adapter_version": adapter.identity().adapter_version,
                    "protocol_version": adapter.protocol_version,
                    "provider": adapter.identity().provider,
                    "agent_name": adapter.identity().agent_name,
                    "missing_capabilities": list(capabilities.missing()),
                }
            )
        return tuple(described)

    # -- internals ---------------------------------------------------------

    def _inspect(self, factory: Callable[[], AgentAdapter]) -> AgentAdapter:
        """Call the factory and refuse an incompatible protocol version."""
        adapter = factory()
        if not isinstance(adapter, AgentAdapter):
            raise AdapterProtocolError(
                f"{type(adapter).__name__} is not an AgentAdapter: it is missing "
                "name/protocol_version/identity/capabilities/start/handle_event/finish"
            )
        if adapter.protocol_version != self._protocol_version:
            raise AdapterProtocolError(
                f"Adapter {adapter.name!r} speaks protocol version "
                f"{adapter.protocol_version!r}, but this build speaks "
                f"{self._protocol_version!r}. Upgrading one side of an integration is "
                "not a scenario a protocol can absorb silently."
            )
        return adapter
