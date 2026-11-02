"""运行配置：工作目录、数据根目录、路径访问控制。

环境变量（均可选）：
  RPS_TOOLS_WORKSPACE    任务产物目录，默认 <repo>/workspace
  RPS_TOOLS_DATA_ROOTS   允许从「服务器路径」读取的数据目录，多个用系统路径分隔符(; 或 :)分开
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def default_data_roots() -> list[Path]:
    roots = [ROOT / "experiments", ROOT / "export"]
    solver_export = ROOT.parent / "rps_solver" / "export"
    if solver_export.is_dir():
        roots.append(solver_export)
    env = os.environ.get("RPS_TOOLS_DATA_ROOTS", "")
    roots += [Path(p) for p in env.split(os.pathsep) if p.strip()]
    return roots


class PathAccessError(ValueError):
    pass


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    workers: int = 2
    retention_days: int = 14
    workspace: Path = field(default_factory=lambda: Path(os.environ.get("RPS_TOOLS_WORKSPACE", ROOT / "workspace")))
    data_roots: list[Path] = field(default_factory=default_data_roots)
    # None = 按 host 自动决定：只监听本机时可读任意路径；对局域网开放时只允许 data_roots
    restrict_paths: bool | None = None

    def __post_init__(self):
        self.workspace = Path(self.workspace).resolve()
        self.data_roots = [Path(p).resolve() for p in self.data_roots]

    @property
    def jobs_dir(self) -> Path:
        return self.workspace / "jobs"

    @property
    def is_restricted(self) -> bool:
        if self.restrict_paths is not None:
            return self.restrict_paths
        return self.host not in LOCAL_HOSTS

    def browse_roots(self) -> list[Path]:
        return [p for p in self.data_roots if p.is_dir()] + [self.jobs_dir]

    def resolve_path(self, raw: str, kind: str = "any") -> Path:
        """把用户填写的服务器路径解析成绝对路径并做访问检查。

        相对路径按仓库根目录解析（所以可以直接写 experiments/<实验名>）。
        kind: "file" / "dir" / "any"
        """
        raw = (raw or "").strip().strip('"')
        if not raw:
            raise PathAccessError("路径为空")
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        p = p.resolve()
        if self.is_restricted and not any(_is_within(p, r) for r in self.browse_roots()):
            allowed = "、".join(str(r) for r in self.browse_roots())
            raise PathAccessError(f"不允许访问该路径：{p}（仅允许：{allowed}）")
        if kind == "file" and not p.is_file():
            raise PathAccessError(f"文件不存在：{p}")
        if kind == "dir" and not p.is_dir():
            raise PathAccessError(f"目录不存在：{p}")
        if kind == "any" and not p.exists():
            raise PathAccessError(f"路径不存在：{p}")
        return p


def _is_within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False
