"""用 Nuitka 把 Web 服务编译成 Windows 目录版，发布包里不含 Python 源码。

用法（需要 Visual Studio 的 C++ 工具集）：
  python scripts/build_windows.py            首次会创建 build/venv 并安装构建依赖
  python scripts/build_windows.py --clean    丢弃上次的编译缓存后重新编译
  python scripts/build_windows.py --mark-deployed
                                             把当前产物记为服务器上的版本，首次整包部署后执行一次

产物：dist/rps_tools_server/ 是完整发布目录，入口是 rps_tools_server.exe。
执行过 --mark-deployed 后，每次编译还会生成 dist/update/，只含相对已部署版本有变化的文件，
复制到服务器后双击其中的 update.bat 即可更新。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
VENV_PY = BUILD / "venv" / "Scripts" / "python.exe"
NUITKA_OUT = BUILD / "nuitka"
RELEASE = ROOT / "dist" / "rps_tools_server"
EXE_NAME = "rps_tools_server.exe"
STATIC = "rps_tools/web/static"
SERVICE_XML = ROOT / "packaging" / "rps_tools_service.xml"
UPDATE_SCRIPTS = [ROOT / "packaging" / "update.ps1", ROOT / "packaging" / "update.bat"]
UPDATE_DIR = ROOT / "dist" / "update"
BASELINE = ROOT / "dist" / "deployed_manifest.json"
MANIFEST_NAME = "rps_tools_release.json"
# 部署后由服务器自己维护的文件，不参与更新。
SERVER_OWNED = {SERVICE_XML.name, "rps_tools_service.exe", MANIFEST_NAME}

BUILD_DEPS = ["nuitka>=2.7", "orjson>=3.9"]
# 这些模块是按字符串导入的，静态分析跟不到，必须显式带上。
NUITKA_INCLUDES = [
    "--include-package=rps_tools",
    "--include-package=uvicorn",
    "--include-package=markdown",
    "--include-module=anyio._backends._asyncio",
    "--include-module=orjson",
]


def run(cmd: list[str]) -> None:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True)


def ensure_venv() -> None:
    if not VENV_PY.exists():
        run([sys.executable, "-m", "venv", BUILD / "venv"])
    run([VENV_PY, "-m", "pip", "--disable-pip-version-check", "install", "-r", ROOT / "requirements.txt", *BUILD_DEPS])


def compile_app() -> Path:
    run([
        VENV_PY, "-m", "nuitka",
        "--standalone",
        "--msvc=latest",
        f"--output-dir={NUITKA_OUT}",
        "--output-filename=" + EXE_NAME,
        "--python-flag=no_docstrings",
        *NUITKA_INCLUDES,
        f"--include-data-dir={STATIC}={STATIC}",
        ROOT / "run_server.py",
    ])
    return NUITKA_OUT / "run_server.dist"


def check_release(release: Path) -> None:
    """发布包里不允许出现源码或内部文档，也不能缺网页文件。"""
    leaked = [p for p in release.rglob("*") if p.suffix.lower() in {".py", ".pyc", ".md"}]
    if leaked:
        raise SystemExit("发布包包含不应发布的文件：\n" + "\n".join(str(p) for p in leaked[:20]))
    for name in ("index.html", "app.js", "app.css"):
        if not (release / STATIC / name).is_file():
            raise SystemExit(f"发布包缺少网页文件：{name}")
    if not (release / EXE_NAME).is_file():
        raise SystemExit(f"发布包缺少 {EXE_NAME}")


def write_manifest(release: Path) -> dict[str, str]:
    files = {}
    for path in sorted(p for p in release.rglob("*") if p.is_file()):
        rel = path.relative_to(release).as_posix()
        if rel not in SERVER_OWNED:
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    (release / MANIFEST_NAME).write_text(json.dumps({"files": files}, indent=1), encoding="utf-8")
    return files


def make_update(release: Path, target: dict[str, str]) -> None:
    """按基线生成增量包；基线落后也安全，服务器会跳过内容已经相同的文件。"""
    if UPDATE_DIR.exists():
        shutil.rmtree(UPDATE_DIR)
    if not BASELINE.exists():
        print("还没有部署基线：首次请整包部署 dist/rps_tools_server，然后执行 --mark-deployed")
        return
    deployed = json.loads(BASELINE.read_text(encoding="utf-8"))["files"]
    changed = [rel for rel, digest in target.items() if deployed.get(rel) != digest]
    removed = [rel for rel in deployed if rel not in target]
    if not changed and not removed:
        print("和已部署版本完全相同，不需要更新")
        return

    files = UPDATE_DIR / "files"
    for rel in changed:
        dest = files / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(release / rel, dest)
    files.mkdir(parents=True, exist_ok=True)
    shutil.copy2(release / MANIFEST_NAME, UPDATE_DIR / MANIFEST_NAME)
    for script in UPDATE_SCRIPTS:
        shutil.copy2(script, UPDATE_DIR / script.name)
    size = sum(p.stat().st_size for p in files.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"增量包：{UPDATE_DIR}（变化 {len(changed)} 个文件，移除 {len(removed)} 个，{size:.0f} MB）")
    for rel in changed:
        print("  " + rel)


def mark_deployed() -> None:
    manifest = RELEASE / MANIFEST_NAME
    if not manifest.is_file():
        raise SystemExit(f"找不到 {manifest}，请先编译")
    shutil.copy2(manifest, BASELINE)
    print(f"已把当前产物记为服务器版本：{BASELINE}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true", help="删除上次的编译缓存")
    ap.add_argument("--mark-deployed", action="store_true", help="把当前产物记为服务器上的版本，不编译")
    args = ap.parse_args()
    if sys.platform != "win32":
        raise SystemExit("这个脚本只用于 Windows")
    if args.mark_deployed:
        mark_deployed()
        return
    if args.clean and NUITKA_OUT.exists():
        shutil.rmtree(NUITKA_OUT)

    ensure_venv()
    compiled = compile_app()
    check_release(compiled)

    if RELEASE.exists():
        shutil.rmtree(RELEASE)
    RELEASE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(compiled, RELEASE)
    shutil.copy2(SERVICE_XML, RELEASE / SERVICE_XML.name)
    target = write_manifest(RELEASE)
    size = sum(p.stat().st_size for p in RELEASE.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"完成：{RELEASE}（{size:.0f} MB）")
    make_update(RELEASE, target)


if __name__ == "__main__":
    main()
