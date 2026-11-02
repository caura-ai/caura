"""Exact Swagger UI release with integrity checks on both external assets."""

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse

# Change the version and both hashes together. Compare the CDN bytes with the
# same npm release before calculating SHA-384; a CDN response alone is not proof.
SWAGGER_VERSION = "5.34.0"
_BASE = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_VERSION}"
_ASSETS = {
    "src": (
        f"{_BASE}/swagger-ui-bundle.js",
        "sha384-9Td7MKiIr2S83E1kZyyVi8KdJw/S8CpAvu0llLyMeg0xq/hDGiZJfVn0fV4llCmK",
    ),
    "href": (
        f"{_BASE}/swagger-ui.css",
        "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF",
    ),
}


def configure_swagger_ui(app: FastAPI) -> None:
    async def swagger_ui(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        response = get_swagger_ui_html(
            openapi_url=f"{root}{app.openapi_url}",
            title=f"{app.title} - Swagger UI",
            swagger_js_url=_ASSETS["src"][0],
            swagger_css_url=_ASSETS["href"][0],
            swagger_ui_parameters=app.swagger_ui_parameters,
            init_oauth=app.swagger_ui_init_oauth,
            oauth2_redirect_url=(
                f"{root}{app.swagger_ui_oauth2_redirect_url}" if app.swagger_ui_oauth2_redirect_url else None
            ),
        )
        html = bytes(response.body).decode()
        for attribute, (url, integrity) in _ASSETS.items():
            original = f'{attribute}="{url}"'
            if html.count(original) != 1:
                raise RuntimeError("Unexpected Swagger template; asset integrity could not be applied")
            html = html.replace(original, f'{original} integrity="{integrity}" crossorigin="anonymous"')
        return HTMLResponse(html)

    app.add_route("/api/docs", swagger_ui, methods=["GET", "HEAD"], include_in_schema=False)
    if app.swagger_ui_oauth2_redirect_url:

        async def oauth_redirect(_request: Request) -> HTMLResponse:
            return get_swagger_ui_oauth2_redirect_html()

        app.add_route(
            app.swagger_ui_oauth2_redirect_url,
            oauth_redirect,
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
