from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from api.i18n import tr


class LocalizedHTTPException(HTTPException):
    def __init__(
        self, status_code: int, code: str, params: dict[str, object] | None = None
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.params = params or {}
        super().__init__(status_code=status_code, detail=tr("ru", code, **self.params))


async def localized_http_exception_handler(
    request: Request, exc: LocalizedHTTPException
) -> JSONResponse:
    language = getattr(request.state, "language", "en")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": tr(language, exc.code, **exc.params),
            "error": {"code": exc.code, "params": exc.params},
        },
    )
