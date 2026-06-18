from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import candidates, resumes, jobs, applications, analytics, auth, companies

app = FastAPI(title="BD Automation API", version="1.0.0")

# CORS — Module 5 (Next.js frontend) needs this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
app.include_router(auth.router)
app.include_router(candidates.router)
app.include_router(resumes.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(analytics.router)
app.include_router(companies.router)

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "module": "data_orchestration"}