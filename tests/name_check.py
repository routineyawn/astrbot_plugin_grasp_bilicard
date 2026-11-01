"""粗查名字用错：模块里引用了、但整个模块从没定义过/导入过的名字。

不追求完美（不做作用域推断），只保证不漏这类真错：
    t0p / locate_ffmpeg 这种「写了但没定义」的名字，编译能过、跑起来才炸。
"""

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "main.py"] + sorted((ROOT / "bili").glob("*.py"))


def defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if not hasattr(node, "args"):
                continue
            args = node.args
            for a in args.args + args.kwonlyargs + args.posonlyargs:
                names.add(a.arg)
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Lambda):
            for a in node.args.args + node.args.kwonlyargs:
                names.add(a.arg)
    return names


def main() -> int:
    bad = 0
    for path in FILES:
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        known = defined_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in known:
                    print(f"{path.name}:{node.lineno} 未定义的名字 {node.id}")
                    bad += 1
    print("名字检查:", "通过" if not bad else f"{bad} 处可疑")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
