"""python -m agent —— 起 agent 服务（默认 8100）。"""

from __future__ import annotations

import uvicorn

from .config import AGENT_PORT

if __name__ == "__main__":
    uvicorn.run("agent.app:app", host="127.0.0.1", port=AGENT_PORT, reload=False)
