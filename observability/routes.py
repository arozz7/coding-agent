from fastapi import FastAPI, Response
from .metrics import MetricsCollector

metrics_collector = MetricsCollector()


def create_metrics_app() -> FastAPI:
    app = FastAPI()

    @app.get("/metrics")
    async def metrics():
        return Response(
            content=metrics_collector.get_metrics(),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    @app.get("/stats")
    async def stats():
        return {"status": "stats_endpoint"}

    return app
