from __future__ import annotations

import uvicorn

from .config import Settings


def run() -> None:
    settings = Settings()  # type: ignore[call-arg]
    uvicorn.run(
        "mt5_signal_service.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
    )


if __name__ == "__main__":
    run()
