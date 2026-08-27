"""allseer launcher.

  python run.py            -> dashboard at http://127.0.0.1:8077
  python run.py --once     -> run research once in the terminal (for Task Scheduler)
  python run.py --reload   -> restart the server on every code edit (development)
"""
import asyncio
import sys
from pathlib import Path

from allseer import db


def main():
    db.init()
    if "--once" in sys.argv:
        from allseer import pipeline
        asyncio.run(pipeline.run_research())
        return
    import uvicorn
    port = 8077
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    # Without this a dashboard started before an edit keeps serving the modules it
    # imported at startup, silently - which reads as a retrieval bug, not a stale server.
    reload = "--reload" in sys.argv
    print("allseer dashboard -> http://127.0.0.1:" + str(port)
          + (" (auto-reload on)" if reload else ""))
    # reload_dirs only when reloading: uvicorn warns about it otherwise.
    extra = {"reload_dirs": [str(Path(__file__).parent / "allseer")]} if reload else {}
    uvicorn.run("allseer.app:app", host="127.0.0.1", port=port, log_level="warning",
                reload=reload, **extra)


if __name__ == "__main__":
    main()
