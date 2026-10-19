import uvicorn

from acmg.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "acmg.webapp:app",
        host=settings.host,
        port=settings.port,
        factory=False,
    )


if __name__ == "__main__":
    main()
