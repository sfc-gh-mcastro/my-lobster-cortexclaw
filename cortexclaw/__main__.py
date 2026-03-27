"""Entry point for ``python -m cortexclaw``."""

import asyncio


def main() -> None:
    from .orchestrator import main as orchestrator_main

    try:
        asyncio.run(orchestrator_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
