"""Service-side pieces that are still specific to one broker.

Broker connections, message shapes and order policy live in the plugins under
``plugins/`` and reach this service through ``ta_plugin_api.ExecutionProvider``.
What remains here is MT5-only service code: OCO groups (until they run on the
provider), the signal file log and operator notifications.
"""
