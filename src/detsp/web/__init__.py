"""本地网页面板：与引擎同进程共享状态，只绑 127.0.0.1，无鉴权。

接口见 server.py；页面见 page.py。启动方式：python -m detsp
（或在 main 里调用 serve）。切勿绑公网或做端口映射。
"""

from .server import DEFAULT_HOST, DEFAULT_PORT, PanelState, serve, web_settings

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "PanelState", "serve", "web_settings"]
