"""Start EdgeMind: Qdrant Server + sync gateway + two tablet processes + dashboard.

    python launch.py              # opens http://127.0.0.1:8000
    python launch.py --no-browser
"""
from app.console import main

if __name__ == "__main__":
    main()
