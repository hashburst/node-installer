import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT)
EXCLUDED = {
    SELF,
    Path("docs/ARCHITECTURE_SCOPE_V220.md"),
}
FORBIDDEN = (
    "k" + "325t",
    "bi" + "3339m",
    "hash" + "strike",
)
TEXT_SUFFIXES = {
    ".py", ".sh", ".json", ".md", ".service", ".conf", ".yml", ".yaml", ".txt",
}


class HashBurstOnlyScopeTests(unittest.TestCase):
    def test_separate_project_terms_do_not_reenter_hashburst_runtime(self):
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if rel in EXCLUDED or ".git" in rel.parts or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            rel_l = rel.as_posix().lower()
            for term in FORBIDDEN:
                if term in rel_l:
                    offenders.append(f"path:{rel}")
            try:
                text = path.read_text(encoding="utf-8").lower()
            except UnicodeDecodeError:
                continue
            for term in FORBIDDEN:
                if term in text:
                    offenders.append(f"content:{rel}:{term}")
        self.assertEqual([], sorted(set(offenders)))


if __name__ == "__main__":
    unittest.main()
