from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings
from app.routers import candidates, resumes, jobs, applications, analytics, auth, companies

settings = get_settings()

def create_app() -> FastAPI:
    app = FastAPI(
        title="BD Automator API",
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

    # Register all routers from Module 1
    app.include_router(auth.router)
    app.include_router(candidates.router)
    app.include_router(resumes.router)
    app.include_router(jobs.router)
    app.include_router(applications.router)
    app.include_router(analytics.router)
    app.include_router(companies.router)

    # Module 2 integration
    try:
        from app.routers.module2_routes import router as module2_router
        app.include_router(module2_router)
    except Exception:
        # ignore if module2 isn't available during imports
        pass

    @app.get("/api/health")
    def health_check():
        return {"status": "ok", "app_name": "BD Automator API", "module": "data_orchestration"}
        
    return app

app = create_app()