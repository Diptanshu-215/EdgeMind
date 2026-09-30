"""Start EdgeMind.

    python launch.py                 # the hub: Qdrant Server + gateway + edge nodes + dashboard
    python launch.py --no-browser
    python launch.py join --hub http://<hub-ip>:8100 --code EM-1234 --name "Line crew C" --sites pune,global
                                     # this laptop becomes an edge node of that hub
"""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "join":
        from app.edge_node import main as join  # sets the node's environment before config loads

        join(sys.argv[2:])
    else:
        from app.console import main

        main()
