"""Broker adapters.

Each adapter owns one broker's connection, message shapes and quirks, and
exposes the same port (see ``execution_service.ports``). Which adapters load is
an environment decision, so the same codebase runs on macOS against cTrader and
on Windows against MetaTrader 5.

Adapter modules are imported lazily, by name, from the ADAPTERS setting. That
laziness is load-bearing rather than tidy: the mt5 adapter imports the
MetaTrader5 package, which has no wheel outside Windows, so a macOS install must
never touch it.
"""
