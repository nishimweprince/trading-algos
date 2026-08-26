"""MetaTrader 5 adapter.

Imported only when ADAPTERS names it. The MetaTrader5 package it depends on has
no wheel outside Windows, so importing this module on macOS or Linux fails by
design rather than by accident.
"""
