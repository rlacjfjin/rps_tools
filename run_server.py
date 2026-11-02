"""快捷启动：python run_server.py [--host 0.0.0.0] [--port 8765] ...

等价于 python -m rps_tools serve ...
"""
import sys

from rps_tools.cli import main

if __name__ == "__main__":
    main(["serve", *sys.argv[1:]])
