"""
刮刮乐远程控制 API 路由
提供 WebSocket 和 HTTP 接口用于远程操作滑块验证
【优化版】：提升滑块通过率核心优化
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
import asyncio
import os
import time
from loguru import logger

from utils.captcha_remote_control import captcha_controller


# 创建路由器
router = APIRouter(prefix="/api/captcha", tags=["captcha"])

# 滑块验证优化配置（可根据实际场景调整）
SLIDER_CONFIG = {
    "POST_UP_WAIT_BASE": 1.8,       # 鼠标释放后基础等待时间（秒），适配闲鱼风控响应延迟
    "POST_UP_WAIT_RANDOM": 0.7,     # 随机偏移时间，模拟人类操作间隔
    "COMPLETION_CHECK_RETRY": 5,    # 验证完成状态轮询次数
    "COMPLETION_CHECK_INTERVAL": 0.4, # 轮询间隔
    "SCREENSHOT_QUALITY": 40,       # 截图质量（平衡速度和清晰度）
    "MAX_SLIDE_RETRY": 2,           # 单次会话最大重试次数
}

class MouseEvent(BaseModel):
    """鼠标事件模型"""
    session_id: str
    event_type: str  # down, move, up
    x: int
    y: int


class SessionCheckRequest(BaseModel):
    """会话检查请求"""
    session_id: str


# =============================================================================
# 辅助函数（新增：优化核心逻辑）
# =============================================================================
async def random_human_delay(base: float, random_range: float) -> float:
    """生成模拟人类的随机延迟（避免固定时间被风控）"""
    import random
    delay = base + random.uniform(0, random_range)
    await asyncio.sleep(delay)
    return delay

async def check_completion_with_retry(session_id: str) -> bool:
    """
    重试机制检查验证完成状态（核心优化：避免单次误判）
    :return: 最终验证状态
    """
    for retry in range(SLIDER_CONFIG["COMPLETION_CHECK_RETRY"]):
        completed = await captcha_controller.check_completion(session_id)
        if completed:
            # 双重确认（防闲鱼临时渲染卡顿）
            await asyncio.sleep(0.2)
            completed = await captcha_controller.check_completion(session_id)
            if completed:
                logger.info(f"✅ 第{retry+1}次检查：验证完成 | Session: {session_id}")
                return True
        logger.debug(f"🔍 第{retry+1}次检查：未完成 | Session: {session_id}")
        await asyncio.sleep(SLIDER_CONFIG["COMPLETION_CHECK_INTERVAL"])
    return False

def get_session_slide_retry(session_id: str) -> int:
    """获取会话的滑块重试次数（避免无限重试触发风控）"""
    session_data = captcha_controller.active_sessions.get(session_id, {})
    return session_data.get("slide_retry_count", 0)

def increment_session_slide_retry(session_id: str):
    """增加会话重试次数"""
    if session_id in captcha_controller.active_sessions:
        captcha_controller.active_sessions[session_id]["slide_retry_count"] = \
            get_session_slide_retry(session_id) + 1

# =============================================================================
# WebSocket 端点 - 实时通信（核心优化区）
# =============================================================================

@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket 连接用于实时传输截图和接收鼠标事件
    优化点：
    1. 延长鼠标释放后等待时间，适配闲鱼滑块响应延迟
    2. 多次轮询检查验证状态，避免单次误判
    3. 增加人类随机延迟，规避风控
    4. 重试次数限制，防止无限重试
    5. 优化截图更新策略，减少无效截图
    6. 增强会话状态校验，避免无效操作
    """
    await websocket.accept()
    logger.info(f"🔌 WebSocket 连接建立: {session_id}")

    # 初始化会话重试次数
    if session_id in captcha_controller.active_sessions:
        captcha_controller.active_sessions[session_id].setdefault("slide_retry_count", 0)

    # 注册 WebSocket 连接
    captcha_controller.websocket_connections[session_id] = websocket

    try:
        # 校验会话有效性（增强版）
        if session_id not in captcha_controller.active_sessions:
            await websocket.send_json({
                'type': 'error',
                'message': '会话不存在',
                'code': 'SESSION_NOT_FOUND'
            })
            await websocket.close(code=1008)
            return

        session_data = captcha_controller.active_sessions[session_id]
        # 发送初始会话信息（优化：携带重试次数）
        await websocket.send_json({
            'type': 'session_info',
            'screenshot': session_data['screenshot'],
            'captcha_info': session_data['captcha_info'],
            'viewport': session_data['viewport'],
            'slide_retry_count': get_session_slide_retry(session_id)
        })

        # 持续接收客户端消息
        slide_success = False
        while True:
            try:
                data = await websocket.receive_json()
            except Exception as e:
                logger.warning(f"❌ 接收客户端消息失败: {e} | Session: {session_id}")
                continue

            msg_type = data.get('type')
            current_retry = get_session_slide_retry(session_id)

            # 超过最大重试次数，提示前端
            if current_retry >= SLIDER_CONFIG["MAX_SLIDE_RETRY"]:
                await websocket.send_json({
                    'type': 'retry_exceed',
                    'message': f'已达到最大重试次数（{SLIDER_CONFIG["MAX_SLIDE_RETRY"]}次），请刷新会话重试',
                    'code': 'RETRY_EXCEED'
                })
                break

            if msg_type == 'mouse_event':
                # 处理鼠标事件（核心优化）
                event_type = data.get('event_type')
                x = data.get('x')
                y = data.get('y')

                # 前置校验：确保坐标在视口内（避免无效操作）
                if session_data.get('viewport'):
                    viewport = session_data['viewport']
                    if not (0 <= x <= viewport.get('width', 0) and 0 <= y <= viewport.get('height', 0)):
                        await websocket.send_json({
                            'type': 'error',
                            'message': '坐标超出验证码区域，请重新操作',
                            'code': 'INVALID_COORDINATE'
                        })
                        continue

                # 处理鼠标事件
                success = await captcha_controller.handle_mouse_event(
                    session_id, event_type, x, y
                )

                if success:
                    # 鼠标释放后核心处理（通过率关键）
                    if event_type == 'up':
                        # 1. 模拟人类操作延迟（随机化）
                        delay = await random_human_delay(
                            SLIDER_CONFIG["POST_UP_WAIT_BASE"],
                            SLIDER_CONFIG["POST_UP_WAIT_RANDOM"]
                        )
                        logger.debug(f"🕒 鼠标释放后等待 {delay:.2f}s | Session: {session_id}")

                        # 2. 多次轮询检查完成状态（核心优化）
                        completed = await check_completion_with_retry(session_id)

                        if completed:
                            # 最终确认 + 通知前端
                            await asyncio.sleep(0.3)
                            completed = await captcha_controller.check_completion(session_id)
                            if completed:
                                await websocket.send_json({
                                    'type': 'completed',
                                    'message': '验证成功！',
                                    'code': 'SUCCESS'
                                })
                                logger.success(f"✅ 验证完成: {session_id} | 重试次数: {current_retry}")
                                slide_success = True
                                break
                        else:
                            # 验证失败：增加重试次数 + 刷新截图 + 提示前端
                            increment_session_slide_retry(session_id)
                            logger.warning(f"⚠️ 验证未通过 | 重试次数: {current_retry+1} | Session: {session_id}")

                            # 优化截图更新：只截取验证码区域，提升速度
                            screenshot = await captcha_controller.update_screenshot(
                                session_id,
                                quality=SLIDER_CONFIG["SCREENSHOT_QUALITY"],
                                only_captcha_area=True  # 假设captcha_remote_control支持该参数
                            )
                            if screenshot:
                                await websocket.send_json({
                                    'type': 'screenshot_update',
                                    'screenshot': screenshot,
                                    'slide_retry_count': current_retry + 1,
                                    'message': '验证未通过，请重新滑动（注意滑动速度和轨迹）'
                                })
                    else:
                        # 按下/移动时：轻量化截图更新（仅在移动间隔>0.1s时更新，减少性能消耗）
                        if event_type in ['down', 'move']:
                            # 移动事件节流：避免高频截图
                            last_move_time = session_data.get('last_move_time', 0)
                            if time.time() - last_move_time > 0.1:
                                screenshot = await captcha_controller.update_screenshot(
                                    session_id,
                                    quality=SLIDER_CONFIG["SCREENSHOT_QUALITY"],
                                    only_captcha_area=True
                                )
                                if screenshot:
                                    await websocket.send_json({
                                        'type': 'screenshot_update',
                                        'screenshot': screenshot
                                    })
                                session_data['last_move_time'] = time.time()

            elif msg_type == 'check_completion':
                # 手动检查完成状态（优化：复用重试检查逻辑）
                completed = await check_completion_with_retry(session_id)
                await websocket.send_json({
                    'type': 'completion_status',
                    'completed': completed,
                    'slide_retry_count': current_retry
                })
                if completed:
                    slide_success = True
                    break

            elif msg_type == 'ping':
                # 心跳（优化：携带会话状态）
                await websocket.send_json({
                    'type': 'pong',
                    'session_exists': session_id in captcha_controller.active_sessions,
                    'slide_retry_count': current_retry
                })

    except WebSocketDisconnect:
        logger.info(f"🔌 WebSocket 连接断开: {session_id}")

    except Exception as e:
        logger.error(f"❌ WebSocket 错误: {e} | Session: {session_id}")
        import traceback
        logger.error(traceback.format_exc())
        # 异常时通知前端
        if websocket.client_state.value == 1:  # 连接仍有效
            await websocket.send_json({
                'type': 'error',
                'message': f'服务器异常：{str(e)}',
                'code': 'SERVER_ERROR'
            })

    finally:
        # 清理资源 + 标记会话状态
        if session_id in captcha_controller.websocket_connections:
            del captcha_controller.websocket_connections[session_id]

        if session_id in captcha_controller.active_sessions:
            captcha_controller.active_sessions[session_id]['completed'] = slide_success
            captcha_controller.active_sessions[session_id]['last_operate_time'] = time.time()

        logger.info(f"🔒 WebSocket 会话结束: {session_id} | 验证成功: {slide_success}")


# =============================================================================
# HTTP 端点 - REST API（兼容原有逻辑 + 少量优化）
# =============================================================================

@router.get("/sessions")
async def get_active_sessions():
    """获取所有活跃的验证会话（优化：携带重试次数）"""
    sessions = []
    for session_id, data in captcha_controller.active_sessions.items():
        sessions.append({
            'session_id': session_id,
            'completed': data.get('completed', False),
            'has_websocket': session_id in captcha_controller.websocket_connections,
            'slide_retry_count': data.get('slide_retry_count', 0),
            'last_operate_time': data.get('last_operate_time', 0)
        })

    return {
        'count': len(sessions),
        'sessions': sessions
    }


@router.get("/session/{session_id}")
async def get_session_info(session_id: str):
    """获取指定会话的信息（优化：携带重试次数）"""
    if session_id not in captcha_controller.active_sessions:
        raise HTTPException(status_code=404, detail="会话不存在")

    session_data = captcha_controller.active_sessions[session_id]

    return {
        'session_id': session_id,
        'screenshot': session_data['screenshot'],
        'captcha_info': session_data['captcha_info'],
        'viewport': session_data['viewport'],
        'completed': session_data.get('completed', False),
        'slide_retry_count': session_data.get('slide_retry_count', 0)
    }


@router.get("/screenshot/{session_id}")
async def get_screenshot(session_id: str):
    """获取最新截图（优化：指定验证码区域 + 质量）"""
    screenshot = await captcha_controller.update_screenshot(
        session_id,
        quality=SLIDER_CONFIG["SCREENSHOT_QUALITY"],
        only_captcha_area=True
    )

    if not screenshot:
        raise HTTPException(status_code=404, detail="无法获取截图")

    return {'screenshot': screenshot}


@router.post("/mouse_event")
async def handle_mouse_event(event: MouseEvent):
    """处理鼠标事件（HTTP方式，不推荐，建议使用WebSocket）"""
    # 前置校验
    if event.session_id not in captcha_controller.active_sessions:
        raise HTTPException(status_code=404, detail="会话不存在")

    current_retry = get_session_slide_retry(event.session_id)
    if current_retry >= SLIDER_CONFIG["MAX_SLIDE_RETRY"]:
        raise HTTPException(status_code=400, detail=f"已达到最大重试次数（{SLIDER_CONFIG['MAX_SLIDE_RETRY']}次）")

    success = await captcha_controller.handle_mouse_event(
        event.session_id,
        event.event_type,
        event.x,
        event.y
    )

    if not success:
        raise HTTPException(status_code=400, detail="处理失败")

    # 检查是否完成（复用重试逻辑）
    completed = False
    if event.event_type == 'up':
        await random_human_delay(SLIDER_CONFIG["POST_UP_WAIT_BASE"], 0)
        completed = await check_completion_with_retry(event.session_id)
        if completed:
            increment_session_slide_retry(event.session_id)

    return {
        'success': True,
        'completed': completed,
        'slide_retry_count': current_retry + (0 if completed else 1)
    }


@router.post("/check_completion")
async def check_completion(request: SessionCheckRequest):
    """检查验证是否完成（优化：复用重试检查逻辑）"""
    if request.session_id not in captcha_controller.active_sessions:
        raise HTTPException(status_code=404, detail="会话不存在")

    completed = await check_completion_with_retry(request.session_id)
    return {
        'session_id': request.session_id,
        'completed': completed,
        'slide_retry_count': get_session_slide_retry(request.session_id)
    }


@router.delete("/session/{session_id}")
async def close_session(session_id: str):
    """关闭会话（优化：清理重试次数）"""
    await captcha_controller.close_session(session_id)
    if session_id in captcha_controller.active_sessions:
        del captcha_controller.active_sessions[session_id]
    return {'success': True}


# =============================================================================
# 前端页面（无核心修改，兼容原有逻辑）
# =============================================================================

@router.get("/status/{session_id}")
async def get_captcha_status(session_id: str):
    """
    获取验证状态
    用于前端轮询检查验证是否完成
    """
    try:
        is_completed = captcha_controller.is_completed(session_id)
        session_exists = captcha_controller.session_exists(session_id)

        return {
            "success": True,
            "completed": is_completed,
            "session_exists": session_exists,
            "session_id": session_id,
            "slide_retry_count": get_session_slide_retry(session_id)
        }
    except Exception as e:
        logger.error(f"获取验证状态失败: {e} | Session: {session_id}")
        return {
            "success": False,
            "completed": False,
            "session_exists": False,
            "session_id": session_id,
            "slide_retry_count": 0,
            "error": str(e)
        }


@router.get("/control", response_class=HTMLResponse)
async def captcha_control_page():
    """返回滑块控制页面"""
    html_file = "captcha_control.html"

    if os.path.exists(html_file):
        return FileResponse(html_file, media_type="text/html")
    else:
        # 返回简单的提示页面
        return HTMLResponse(content="""
        <!DOCTYPE html>
        <html>
        <head>
            <title>验证码控制面板</title>
        </head>
        <body>
            <h1>验证码控制面板</h1>
            <p>前端页面文件 captcha_control.html 不存在</p>
            <p>请查看文档了解如何创建前端页面</p>
        </body>
        </html>
        """)


@router.get("/control/{session_id}", response_class=HTMLResponse)
async def captcha_control_page_with_session(session_id: str):
    """返回带会话ID的滑块控制页面（优化：注入重试配置）"""
    html_file = "captcha_control.html"

    if os.path.exists(html_file):
        with open(html_file, 'r', encoding='utf-8') as f:
            html_content = f.read()
            # 注入会话ID + 重试配置
            inject_script = f"""
            <script>
                window.INITIAL_SESSION_ID = "{session_id}";
                window.SLIDER_CONFIG = {SLIDER_CONFIG};
            </script>
            </body>
            """
            html_content = html_content.replace('</body>', inject_script)
            return HTMLResponse(content=html_content)
    else:
        raise HTTPException(status_code=404, detail="前端页面不存在")