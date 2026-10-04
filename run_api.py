"""启动标准意见协同后端：python run_api.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn

from standards_collaboration.api import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
