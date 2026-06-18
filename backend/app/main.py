from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        description="FastAPI Central Orchestrator for BD Automator Agent",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health_check():
        return {"status": "ok", "app_name": settings.APP_NAME}

    # Routers will be registered here by team members
    # app.include_router(candidates.router, prefix="/api/candidates", tags=["Candidates"])
    # app.include_router(jobs.router, prefix="/api/jobs", tags=["Jobs"])
    
    return app

app = create_app()
