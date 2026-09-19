import subprocess
import unittest
from pathlib import Path


class HashBurstOnlyBoundaryTests(unittest.TestCase):
    def test_external_project_marker_is_absent_from_tracked_tree(self):
        repository = Path(__file__).resolve().parents[1]
        marker = ("k325" + "t").encode("ascii")

        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "-z",
            ],
            check=True,
            stdout=subprocess.PIPE,
        )

        tracked_paths = [
            Path(raw.decode("utf-8"))
            for raw in completed.stdout.split(b"\0")
            if raw
        ]

        path_matches = [
            str(relative)
            for relative in tracked_paths
            if marker.decode("ascii") in str(relative).lower()
        ]

        content_matches = []

        for relative in tracked_paths:
            absolute = repository / relative

            if not absolute.is_file():
                continue

            content = absolute.read_bytes().lower()

            if marker in content:
                content_matches.append(str(relative))

        self.assertEqual(
            path_matches,
            [],
            "external-project marker found in tracked paths",
        )
        self.assertEqual(
            content_matches,
            [],
            "external-project marker found in tracked contents",
        )


if __name__ == "__main__":
    unittest.main()
