"""Console entry point for the API service."""

import uvicorn


def run() -> None:
    """Start the development API server."""

    uvicorn.run("computer_use.api.app:app", host="127.0.0.1", port=8000)
