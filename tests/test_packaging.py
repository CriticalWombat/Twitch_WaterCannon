"""The Docker image only contains the files its COPY lines name, so a module that
main.py imports but the Dockerfile forgets crashes the cannon at startup."""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


class PackagingTests(unittest.TestCase):
    def copied_files(self):
        files = set()
        for line in read("Dockerfile").splitlines():
            parts = line.split()
            if parts and parts[0] == "COPY":
                files.update(parts[1:-1])
        return files

    def test_every_local_module_main_imports_is_in_the_image(self):
        local = {f[:-3] for f in os.listdir(REPO) if f.endswith(".py") and f != "main.py"}
        imported = set(re.findall(r"^(?:import|from)\s+(\w+)", read("main.py"), re.M))
        missing = sorted(m + ".py" for m in local & imported if m + ".py" not in self.copied_files())
        self.assertEqual(missing, [], f"Dockerfile does not COPY: {missing}")

    def test_overlay_page_is_in_the_image(self):
        self.assertIn("overlay.html", self.copied_files())

    def test_timezone_database_is_a_requirement_or_in_the_base_image(self):
        # zoneinfo needs tzdata; python:3.12-slim ships it, but pin it in case that changes.
        self.assertIn("tzdata", read("requirements.txt"))


if __name__ == "__main__":
    unittest.main()
