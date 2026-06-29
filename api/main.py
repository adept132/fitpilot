from fastapi import FastAPI

from api.routers.splits import router as splits_router
from api.routers.exercises import router as exercises_router
from api.routers.auth import router as auth_router
from api.routers.workout_center import router as workout_center_router
from api.routers.workouts import router as workouts_router
from api.routers.progress import router as progress_router
from api.routers.profile import router as profile_router
from api.routers.workout_supersets import router as workout_supersets_router
from api.routers.mesocycles import router as mesocycles_router
from api.routers.microcycles import router as microcycles_router
from api.routers.plans import router as plans_router
from api.routers.calendar import router as calendar_router

app = FastAPI(title="FitPilot API")

app.include_router(exercises_router, prefix="/exercises", tags=["exercises"])
app.include_router(auth_router)
app.include_router(workout_center_router)
app.include_router(workouts_router)
app.include_router(splits_router)
app.include_router(mesocycles_router)
app.include_router(microcycles_router)
app.include_router(plans_router)
app.include_router(progress_router)
app.include_router(exercises_router)
app.include_router(profile_router)
app.include_router(calendar_router)
app.include_router(workout_supersets_router)

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}
