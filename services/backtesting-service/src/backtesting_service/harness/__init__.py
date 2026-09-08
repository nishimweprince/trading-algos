"""Mode-neutral backtest machinery: fills, costs, sizing, metrics, units, validation.

These six were flat modules beside the engine. They share the property that
makes them a package: the dependency runs strictly one way. Nothing here imports
``engine``, and nothing here imports anything else in ``harness`` -- ``costs``,
``sizing``, ``metrics`` and ``units`` are stdlib-only, and ``fills`` and
``validation`` reach no further than ``models``.

Deliberately no re-exports. Importers name the module they want
(``from .harness.costs import ...``), which keeps that one-way property visible
and keeps ``models._valid_cost_surface`` from closing a
``models -> harness -> fills -> models`` cycle.
"""
