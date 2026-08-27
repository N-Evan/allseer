"""allseer launcher.

  python run.py            -> dashboard at http://127.0.0.1:8077
  python run.py --once     -> run research once in the terminal (for Task Scheduler)
"""
import asyncio
import sys

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
    print("allseer dashboard -> http://127.0.0.1:" + str(port))
    uvicorn.run("allseer.app:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
