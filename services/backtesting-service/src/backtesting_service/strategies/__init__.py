"""Strategy execution modules, one per registered strategy.

``session_hedge`` owns the incumbent's mode behaviour; the engine calls through
to it. Strategy number two adds a module here instead of forking the engine.
"""
