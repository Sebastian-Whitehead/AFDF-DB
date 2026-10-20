from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import engine, get_session
from .routers.accounts import router as accounts_router
from .routers.casino import router as casino_router
from .routers.transactions import router as transactions_router

app = FastAPI(title="Economy Service", version="0.1.0")


@app.on_event("shutdown")
async def shutdown() -> None:
    await engine.dispose()


@app.middleware("http")
async def request_id(request: Request, call_next):
    correlation_id = request.headers.get("X-Request-ID")
    if correlation_id is None:
        correlation_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = correlation_id
    return response


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as error:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": str(error)},
        )
    return {"status": "ready"}


app.include_router(accounts_router)
app.include_router(transactions_router)
app.include_router(casino_router)
