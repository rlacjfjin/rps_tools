"""快捷启动：python run_server.py [--host 0.0.0.0] [--port 8765] ...

等价于 python -m rps_tools serve ...；也是 Windows 打包版的入口。
"""
import multiprocessing
import sys

from rps_tools.cli import main

if __name__ == "__main__":
    # 任务在 spawn 子进程里运行，打包成 exe 后子进程要靠它走回 worker 而不是再起一个服务。
    multiprocessing.freeze_support()
    main(["serve", *sys.argv[1:]])
