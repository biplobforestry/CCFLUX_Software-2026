"""Every capture_exception call must match the signature.

A call that passed the exception where the component belongs raised TypeError
from inside an except block, so a FLIR level-2 failure that was meant to become
a warning failed the whole job instead. The signature takes component, message
and exception positionally, so the shape is worth checking across the tree.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSITIONAL = {"component", "message", "exception"}


def _sources():
    for path in sorted(ROOT.rglob("*.py")):
        if any(part.startswith(".venv") for part in path.parts):
            continue
        yield path


class CaptureExceptionCallSitesTests(unittest.TestCase):
    def test_every_call_passes_component_message_exception_positionally(self):
        problems = []
        for path in _sources():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr == "capture_exception"):
                    continue
                where = f"{path.relative_to(ROOT)}:{node.lineno}"
                if len(node.args) != 3:
                    problems.append(f"{where} passes {len(node.args)} positional arguments")
                named = {keyword.arg for keyword in node.keywords} & POSITIONAL
                if named:
                    problems.append(f"{where} names {sorted(named)} that must be positional")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
