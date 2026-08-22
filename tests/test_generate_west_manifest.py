import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import generate_west_manifest as generator  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class GenerateWestManifestTest(unittest.TestCase):
    def test_parse_west_manifest_repositories_supports_remote_and_url(self):
        manifest = textwrap.dedent(
            """
        manifest:
          defaults:
            remote: upstream
          remotes:
            - name: upstream
              url-base: https://github.com/zephyrproject-rtos
          projects:
            - name: cmsis
              revision: abc
            - name: direct
              url: https://github.com/example/direct
              revision: main
            - name: renamed
              remote: upstream
              repo-path: custom-repo
              revision: main
            """
        )

        self.assertEqual(
            generator.parse_west_manifest_repositories(manifest),
            {
                ("github.com", "zephyrproject-rtos/cmsis"),
                ("github.com", "example/direct"),
                ("github.com", "zephyrproject-rtos/custom-repo"),
            },
        )

    def test_find_modules_excludes_west_manifest_projects(self):
        markdown = """
        [existing](https://github.com/example/existing)
        [external](https://github.com/example/external)
        """

        class FakeClient:
            def find_module(self, repository):
                return generator.Module(
                    repository.path.rsplit("/", 1)[-1],
                    repository.url,
                    "main",
                )

        modules = generator.find_modules(
            markdown,
            FakeClient(),
            {("github.com", "example/existing")},
        )
        self.assertEqual(
            modules,
            [generator.Module("external", "https://github.com/example/external", "main")],
        )

    def test_extract_and_normalize_links(self):
        markdown = """
        [module](https://github.com/example/module)
        [same repo](https://github.com/example/module/tree/main/docs)
        ![image](https://github.com/example/not-a-repository)
        [gitlab](https://gitlab.com/group/subgroup/project)
        [site](https://example.com/project)
        """

        self.assertEqual(
            generator.extract_links(markdown),
            [
                "https://github.com/example/module",
                "https://github.com/example/module/tree/main/docs",
                "https://gitlab.com/group/subgroup/project",
                "https://example.com/project",
            ],
        )
        self.assertEqual(
            generator.normalize_repository(
                "https://github.com/example/module/tree/main/docs"
            ).url,
            "https://github.com/example/module",
        )
        self.assertEqual(
            generator.normalize_repository(
                "https://gitlab.com/group/subgroup/project"
            ).path,
            "group/subgroup/project",
        )

    def test_find_modules_checks_marker_and_deduplicates(self):
        markdown = """
        [one](https://github.com/example/one)
        [one again](https://github.com/example/one/tree/main/docs)
        [two](https://github.com/example/two)
        [gitlab](https://gitlab.com/group/project)
        """

        def fake_urlopen(request, timeout):
            url = request.full_url
            path = urlsplit(url).path
            if path == "/repos/example/one":
                return FakeResponse({"name": "one", "default_branch": "main"})
            if path == "/repos/example/one/contents/zephyr/module.yml":
                return FakeResponse({"type": "file"})
            if path == "/repos/example/two":
                return FakeResponse({"name": "two", "default_branch": "main"})
            if path == "/repos/example/two/contents/zephyr/module.yml":
                return FakeResponse({"type": "directory"})
            if path == "/api/v4/projects/group%2Fproject":
                return FakeResponse({"path": "project", "default_branch": "main"})
            if path == "/api/v4/projects/group%2Fproject/repository/files/zephyr%2Fmodule.yml":
                return FakeResponse({"file_name": "module.yml"})
            raise AssertionError(f"unexpected URL: {url}")

        with patch.object(generator.urllib.request, "urlopen", fake_urlopen):
            modules = generator.find_modules(markdown, generator.ApiClient("token"))

        self.assertEqual(
            modules,
            [
                generator.Module("one", "https://github.com/example/one", "main"),
                generator.Module("project", "https://gitlab.com/group/project", "main"),
            ],
        )

    def test_render_empty_and_nonempty_manifests(self):
        self.assertEqual(
            generator.render_west_manifest([]),
            "# Generated from README.md by generate_west_manifest.py.\n"
            "manifest:\n"
            "  projects: []\n",
        )
        rendered = generator.render_west_manifest(
            [generator.Module("one", "https://github.com/example/one", "main")]
        )
        self.assertIn("name: 'one'", rendered)
        self.assertIn("url: 'https://github.com/example/one'", rendered)
        self.assertIn("revision: 'main'", rendered)

    def test_main_writes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            output = root / "nested" / "external-modules.yml"
            readme.write_text("[site](https://example.com)", encoding="utf-8")

            with patch.object(
                generator.ApiClient,
                "load_west_manifest_repositories",
                return_value=set(),
            ), patch.object(
                sys,
                "argv",
                ["generate_west_manifest.py", "--readme", str(readme), "--output", str(output)],
            ):
                self.assertEqual(generator.main(), 0)
            self.assertTrue(output.is_file())
            self.assertIn("projects: []", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
