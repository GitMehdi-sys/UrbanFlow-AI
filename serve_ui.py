"""Compatibility launcher: always serve the complete application."""
from web.app import app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('web.app:app', host='127.0.0.1', port=8000)
