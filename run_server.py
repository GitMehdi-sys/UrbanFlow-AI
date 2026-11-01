import os, sys
# Ensure the UrbAI package root is on sys.path
project_root = os.path.abspath(os.path.dirname(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000, log_level="info")

