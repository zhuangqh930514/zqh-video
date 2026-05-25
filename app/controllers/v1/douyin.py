from fastapi import Query, Request
from fastapi.responses import HTMLResponse

from app.controllers.v1.base import new_router
from app.models.schema import BaseResponse, DouyinPublishRequest
from app.services.douyin import douyin_service
from app.utils import utils

router = new_router()


@router.get("/douyin/status", response_model=BaseResponse, summary="Get Douyin authorization status")
def get_douyin_status():
    return utils.get_response(200, douyin_service.get_status())


@router.get("/douyin/auth/url", response_model=BaseResponse, summary="Get Douyin OAuth URL")
def get_douyin_auth_url():
    if not douyin_service.is_configured():
        return utils.get_response(400, {"error": "Douyin is not configured"})
    return utils.get_response(200, {"auth_url": douyin_service.get_auth_url()})


@router.get("/douyin/oauth/callback", summary="Douyin OAuth callback")
def douyin_oauth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
):
    if error:
        message = error_description or error
        html = f"""
        <html><body style="font-family:sans-serif;padding:24px;">
        <h2>抖音授权失败</h2>
        <p>{message}</p>
        <p>请关闭此页面，返回 MoneyPrinterTurbo 重新尝试。</p>
        </body></html>
        """
        return HTMLResponse(content=html, status_code=400)

    if not code:
        html = """
        <html><body style="font-family:sans-serif;padding:24px;">
        <h2>抖音授权失败</h2>
        <p>未收到授权码 code。</p>
        </body></html>
        """
        return HTMLResponse(content=html, status_code=400)

    result = douyin_service.exchange_code(code)
    if result.get("success"):
        html = f"""
        <html><body style="font-family:sans-serif;padding:24px;">
        <h2>抖音授权成功</h2>
        <p>账号 open_id: {result.get("open_id", "")}</p>
        <p>可以关闭此页面，返回 MoneyPrinterTurbo 继续发布视频。</p>
        </body></html>
        """
        return HTMLResponse(content=html)

    error_msg = result.get("error", "Unknown error")
    html = f"""
    <html><body style="font-family:sans-serif;padding:24px;">
    <h2>抖音授权失败</h2>
    <p>{error_msg}</p>
    <p>请检查 client_key、client_secret、redirect_uri 是否与开放平台配置一致。</p>
    </body></html>
    """
    return HTMLResponse(content=html, status_code=400)


@router.post("/douyin/publish", response_model=BaseResponse, summary="Publish a video to Douyin")
def publish_video_to_douyin(body: DouyinPublishRequest):
    result = douyin_service.publish_video(body.video_path, body.title)
    status = 200 if result.get("success") else 400
    return utils.get_response(status, result)
