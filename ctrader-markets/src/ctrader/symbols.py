"""Symbol identity resolved once, at the broker boundary.

Provider ids never leak past this module: everything downstream speaks the exact
cTrader symbolName configured in SYMBOLS. Modelled on lookup-trader's
app/providers/instruments.py, including its fail-closed posture — an unknown or
ambiguous symbol raises rather than being silently skipped.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from errors import SymbolResolutionError
from models import SymbolInfo


class SymbolCatalog:
    """Immutable name <-> id mapping plus the per-symbol digits used for scaling."""

    def __init__(self, entries: Iterable[SymbolInfo]) -> None:
        entries = tuple(entries)

        duplicates = [
            name for name, count in Counter(entry.symbol for entry in entries).items() if count > 1
        ]
        if duplicates:
            raise SymbolResolutionError(
                f"Broker returned duplicate symbol names: {sorted(duplicates)}"
            )

        self._by_name: dict[str, SymbolInfo] = {entry.symbol: entry for entry in entries}
        self._by_id: dict[int, SymbolInfo] = {entry.symbol_id: entry for entry in entries}

    @classmethod
    def build(
        cls,
        *,
        requested: Iterable[str],
        light_symbols: Iterable[object],
        digits_by_id: Mapping[int, int],
    ) -> SymbolCatalog:
        """Resolve the configured names against a ProtoOASymbolsListRes.

        `light_symbols` are ProtoOALightSymbol values; `digits_by_id` comes from
        the follow-up ProtoOASymbolByIdRes, because ProtoOALightSymbol carries no
        digits and prices cannot be scaled without them.
        """
        requested = tuple(requested)
        available: dict[str, object] = {}
        for symbol in light_symbols:
            name = str(getattr(symbol, "symbolName", "")).strip()
            if name:
                available.setdefault(name, symbol)

        missing = [name for name in requested if name not in available]
        if missing:
            raise SymbolResolutionError(
                f"Broker does not expose these configured symbols: {sorted(missing)}. "
                "Run --discover-symbols and use the exact symbolName values."
            )

        entries = []
        for name in requested:
            symbol = available[name]
            symbol_id = int(symbol.symbolId)
            if symbol_id not in digits_by_id:
                raise SymbolResolutionError(
                    f"No digits returned for symbol {name!r} (symbolId {symbol_id}); "
                    "prices cannot be scaled without them"
                )
            entries.append(
                SymbolInfo(
                    symbol=name,
                    symbol_id=symbol_id,
                    digits=int(digits_by_id[symbol_id]),
                    enabled=bool(getattr(symbol, "enabled", True)),
                    description=str(getattr(symbol, "description", "")) or None,
                )
            )
        return cls(entries)

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._by_name

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def ids(self) -> list[int]:
        return [self._by_name[name].symbol_id for name in sorted(self._by_name)]

    def entries(self) -> tuple[SymbolInfo, ...]:
        return tuple(self._by_name[name] for name in sorted(self._by_name))

    def info(self, symbol: str) -> SymbolInfo:
        try:
            return self._by_name[symbol]
        except KeyError as exc:
            raise SymbolResolutionError(
                f"Unknown symbol {symbol!r}; configured symbols are {list(self.names())}"
            ) from exc

    def id_for(self, symbol: str) -> int:
        return self.info(symbol).symbol_id

    def digits_for(self, symbol: str) -> int:
        return self.info(symbol).digits

    def name_for_id(self, symbol_id: int) -> str:
        try:
            return self._by_id[symbol_id].symbol
        except KeyError as exc:
            raise SymbolResolutionError(f"Unknown symbolId {symbol_id}") from exc

    def digits_for_id(self, symbol_id: int) -> int:
        try:
            return self._by_id[symbol_id].digits
        except KeyError as exc:
            raise SymbolResolutionError(f"Unknown symbolId {symbol_id}") from exc

    def has_id(self, symbol_id: int) -> bool:
        return symbol_id in self._by_id

    def resolve_many(self, symbols: Iterable[str]) -> frozenset[str]:
        """Validate a caller-supplied selection, failing on the first unknown."""
        requested = frozenset(symbols)
        unknown = sorted(requested - set(self._by_name))
        if unknown:
            raise SymbolResolutionError(
                f"Unknown symbols {unknown}; configured symbols are {list(self.names())}"
            )
        return requested
