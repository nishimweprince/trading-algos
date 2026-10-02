from __future__ import annotations

from ta_core import base_parser, load_or_exit, serve

from .app import LOGGER_NAME, create_app
from .config import load_settings


def run(argv: list[str] | None = None) -> None:
    parser = base_parser("OFI scalper: Binance USDⓈ-M recorder, features, gate and hard risk")
    args = parser.parse_args(argv)
    settings = load_or_exit(load_settings, args.profile)

    def app_factory() -> object:
        return create_app(settings=settings)

    serve(settings, app_factory, logger_name=LOGGER_NAME)


if __name__ == "__main__":
    run()
