"""The Railway image COPYs an explicit file list: every local module a baked script imports must be in it."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def copied_files():
    out = set()
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*COPY\s+(.+?)\s+\./?\s*$", line)
        if m:
            out |= {p for p in m.group(1).split() if p.endswith(".py")}
    return out


def local_imports(path):
    local = {p.stem for p in ROOT.glob("*.py")}
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return {n for n in names if n in local}


class TestDockerfileModules(unittest.TestCase):
    def test_every_local_import_is_copied(self):
        baked = copied_files()
        todo, seen = list(baked), set()
        missing = {}
        while todo:
            f = todo.pop()
            if f in seen or not (ROOT / f).exists():
                continue
            seen.add(f)
            for mod in local_imports(ROOT / f):
                name = mod + ".py"
                if name not in baked:
                    missing.setdefault(name, []).append(f)
                todo.append(name)
        self.assertEqual(missing, {}, f"imported but not COPYed into the image: {missing}")

    def test_serve_worker_scripts_are_copied(self):
        src = (ROOT / "serve.py").read_text(encoding="utf-8")
        workers = set(re.findall(r'_run_worker\(\s*"([a-z_]+\.py)"', src))
        self.assertTrue(workers)
        self.assertEqual(workers - copied_files(), set())


if __name__ == "__main__":
    unittest.main()
