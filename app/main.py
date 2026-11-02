from fastapi import FastAPI

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    verify_production_hardening(settings)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Telegram bot that logs expenses from receipts and text.",
        debug=settings.debug,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/", tags=["root"], summary="API root")
    def root() -> dict[str, str]:
        body = {"app": settings.app_name, "version": __version__}
        if settings.docs_enabled:
            body["docs"] = "/docs"
        return body

    return app


def verify_production_hardening(settings: Settings) -> None:
    """Refuse to boot a production instance with development-grade settings."""
    if not settings.is_production:
        return

    problems: list[str] = []
    if settings.debug:
        problems.append("DEBUG must be false (it exposes tracebacks)")
    if settings.docs_enabled:
        problems.append("DOCS_ENABLED must be false")
    if not settings.telegram_webhook_secret:
        problems.append("TELEGRAM_WEBHOOK_SECRET must be set")

    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


app = create_app()
