from __future__ import annotations

import uvicorn


def run() -> None:
    uvicorn.run(
        "mt5_signal_service.api:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        workers=1,
    )


if __name__ == "__main__":
    run()
