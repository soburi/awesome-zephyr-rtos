#!/usr/bin/env python3
"""Generate a west manifest from repositories linked in a Markdown file."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LINK_RE = re.compile(
    r"(?<!!)\[[^\]]+\]\(\s*(?:<(?P<bracket>[^>]+)>|(?P<plain>[^)\s]+))"
)
USER_AGENT = "awesome-zephyr-rtos-west-manifest"
DEFAULT_ZEPHYR_MANIFEST_URL = (
    "https://api.github.com/repos/zephyrproject-rtos/zephyr/contents/west.yml"
    "?ref=main"
)


class RemoteCheckError(RuntimeError):
    """Raised when a repository cannot be checked reliably."""


@dataclass(frozen=True)
class Repository:
    """A supported repository link normalized to its hosting service."""

    host: str
    path: str
    url: str

    @property
    def key(self) -> tuple[str, str]:
        return self.host, self.path.lower()


@dataclass(frozen=True)
class Module:
    """A repository containing the Zephyr module marker file."""

    name: str
    url: str
    revision: str


def extract_links(markdown: str) -> list[str]:
    """Return unique HTTP(S) Markdown links in source order."""

    links: list[str] = []
    seen: set[str] = set()
    for match in LINK_RE.finditer(markdown):
        url = match.group("bracket") or match.group("plain")
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url not in seen:
            links.append(url)
            seen.add(url)
    return links


def normalize_repository(url: str) -> Repository | None:
    """Normalize a GitHub or GitLab repository URL, if it looks like one."""

    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]

    if host == "github.com":
        if len(parts) < 2:
            return None
        owner, repo = parts[:2]
        repo = repo.removesuffix(".git")
        if not owner or not repo:
            return None
        path = f"{owner}/{repo}"
    elif host == "gitlab.com":
        if len(parts) < 2:
            return None
        if "-" in parts:
            parts = parts[: parts.index("-")]
        path = "/".join(parts).removesuffix(".git")
        if "/" not in path:
            return None
    else:
        return None

    return Repository(host=host, path=path, url=f"https://{host}/{path}")


class ApiClient:
    """Small JSON API client with the headers needed by GitHub and GitLab."""

    def __init__(self, github_token: str | None = None):
        self.github_token = github_token

    def get_json(self, url: str, *, service: str) -> Any:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if service == "github" and self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if service == "github" and error.code in (403, 429):
                raise RemoteCheckError(
                    f"GitHub API rate limit or permission error while checking {url}. "
                    "Set GITHUB_TOKEN when running locally."
                ) from error
            raise RemoteCheckError(
                f"{service.capitalize()} API returned HTTP {error.code} for {url}."
            ) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RemoteCheckError(f"Could not check {url}: {error}") from error

    def get_text(self, url: str) -> str:
        request = urllib.request.Request(
            url, headers={"Accept": "text/plain", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RemoteCheckError(
                f"Manifest URL returned HTTP {error.code}: {url}"
            ) from error
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
            raise RemoteCheckError(f"Could not read manifest {url}: {error}") from error

    def load_west_manifest_exclusions(
        self, url: str
    ) -> tuple[set[tuple[str, str]], set[str]]:
        if url.startswith("https://api.github.com/"):
            payload = self.get_json(url, service="github")
            if not isinstance(payload, dict):
                raise RemoteCheckError(
                    f"GitHub API returned no west manifest content: {url}"
                )
            content = payload.get("content")
            encoding = payload.get("encoding")
            if not isinstance(content, str) or encoding != "base64":
                raise RemoteCheckError(
                    f"GitHub API returned an unsupported west manifest response: {url}"
                )
            try:
                manifest = base64.b64decode(content).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise RemoteCheckError(
                    f"Could not decode the west manifest returned by {url}: {error}"
                ) from error
        else:
            manifest = self.get_text(url)

        repositories = parse_west_manifest_repositories(manifest)
        project_names = parse_west_manifest_project_names(manifest)
        if not repositories:
            raise RemoteCheckError(
                f"No projects were found in the west manifest: {url}"
            )
        return repositories, project_names

    def find_module(self, repository: Repository) -> Module | None:
        if repository.host == "github.com":
            return self._find_github_module(repository)
        if repository.host == "gitlab.com":
            return self._find_gitlab_module(repository)
        raise AssertionError(f"Unsupported repository host: {repository.host}")

    def _find_github_module(self, repository: Repository) -> Module | None:
        api_path = urllib.parse.quote(repository.path, safe="/")
        metadata = self.get_json(
            f"https://api.github.com/repos/{api_path}", service="github"
        )
        if metadata is None:
            return None

        marker = self.get_json(
            f"https://api.github.com/repos/{api_path}/contents/zephyr/module.yml",
            service="github",
        )
        if not isinstance(marker, dict) or marker.get("type") != "file":
            return None

        revision = metadata.get("default_branch")
        if not isinstance(revision, str) or not revision:
            raise RemoteCheckError(
                f"GitHub repository metadata for {repository.url} has no default branch."
            )
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            name = repository.path.rsplit("/", 1)[-1]
        return Module(name=name, url=repository.url, revision=revision)

    def _find_gitlab_module(self, repository: Repository) -> Module | None:
        encoded_path = urllib.parse.quote(repository.path, safe="")
        metadata = self.get_json(
            f"https://gitlab.com/api/v4/projects/{encoded_path}", service="gitlab"
        )
        if metadata is None:
            return None

        revision = metadata.get("default_branch")
        if not isinstance(revision, str) or not revision:
            raise RemoteCheckError(
                f"GitLab repository metadata for {repository.url} has no default branch."
            )
        marker = self.get_json(
            "https://gitlab.com/api/v4/projects/"
            f"{encoded_path}/repository/files/zephyr%2Fmodule.yml"
            f"?ref={urllib.parse.quote(revision, safe='')}",
            service="gitlab",
        )
        if marker is None:
            return None

        name = metadata.get("path")
        if not isinstance(name, str) or not name:
            name = repository.path.rsplit("/", 1)[-1]
        return Module(name=name, url=repository.url, revision=revision)


def parse_west_manifest_repositories(manifest: str) -> set[tuple[str, str]]:
    """Extract repository keys from a west manifest.

    This intentionally parses only the west manifest fields needed for
    comparison. It supports both direct project ``url`` entries and the
    usual ``remote``/``url-base`` form used by Zephyr's manifest.
    """

    remotes: dict[str, str] = {}
    projects: list[dict[str, str]] = []
    defaults_remote: str | None = None
    section: str | None = None
    current: dict[str, str] | None = None
    in_defaults = False

    def finish_project() -> None:
        if section == "projects" and current is not None:
            projects.append(current.copy())

    for raw_line in manifest.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if re.match(r"^\s{2}remotes:\s*$", line):
            finish_project()
            current = None
            section = "remotes"
            in_defaults = False
            continue
        if re.match(r"^\s{2}projects:\s*$", line):
            finish_project()
            current = None
            section = "projects"
            in_defaults = False
            continue
        if re.match(r"^\s{2}defaults:\s*$", line):
            finish_project()
            current = None
            section = None
            in_defaults = True
            continue

        if in_defaults:
            match = re.match(r"^\s{4}remote:\s*(.+?)\s*$", line)
            if match:
                defaults_remote = parse_yaml_scalar(match.group(1))
            continue

        if section == "remotes":
            match = re.match(r"^\s{4}-\s+name:\s*(.+?)\s*$", line)
            if match:
                current = {"name": parse_yaml_scalar(match.group(1))}
                remotes[current["name"]] = ""
                continue
            match = re.match(r"^\s{6}url-base:\s*(.+?)\s*$", line)
            if match and current is not None:
                remotes[current["name"]] = parse_yaml_scalar(match.group(1))
            continue

        if section == "projects":
            match = re.match(r"^\s{4}-\s+name:\s*(.+?)\s*$", line)
            if match:
                finish_project()
                current = {"name": parse_yaml_scalar(match.group(1))}
                continue
            match = re.match(
                r"^\s{6}(remote|repo-path|url):\s*(.+?)\s*$", line
            )
            if match and current is not None:
                current[match.group(1)] = parse_yaml_scalar(match.group(2))

    finish_project()

    repositories: set[tuple[str, str]] = set()
    for project in projects:
        project_url = project.get("url")
        if project_url is None:
            remote_name = project.get("remote", defaults_remote)
            url_base = remotes.get(remote_name or "")
            if not url_base:
                continue
            project_url = f"{url_base.rstrip('/')}/{project.get('repo-path', project['name'])}"
        repository = normalize_repository(project_url)
        if repository is not None:
            repositories.add(repository.key)
    return repositories


def parse_west_manifest_project_names(manifest: str) -> set[str]:
    """Extract project names from the projects section of a west manifest."""

    names: set[str] = set()
    in_projects = False
    for raw_line in manifest.splitlines():
        line = raw_line.rstrip()
        if re.match(r"^\s{2}projects:\s*$", line):
            in_projects = True
            continue
        if in_projects and re.match(r"^\s{2}\w[\w-]*:\s*$", line):
            if not re.match(r"^\s{2}projects:\s*$", line):
                break
        if in_projects:
            match = re.match(r"^\s{4}-\s+name:\s*(.+?)\s*$", line)
            if match:
                names.add(parse_yaml_scalar(match.group(1)).lower())
    return names


def parse_yaml_scalar(value: str) -> str:
    """Parse the quoted scalar forms used for west manifest fields."""

    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value.split(" #", 1)[0].rstrip()


def find_modules(
    markdown: str,
    client: ApiClient,
    excluded_repositories: set[tuple[str, str]] | None = None,
    excluded_project_names: set[str] | None = None,
) -> list[Module]:
    """Check linked repositories, excluding entries in another manifest."""

    repositories: list[Repository] = []
    seen: set[tuple[str, str]] = set()
    for link in extract_links(markdown):
        repository = normalize_repository(link)
        repository_name = (
            repository.path.rsplit("/", 1)[-1].lower()
            if repository is not None
            else None
        )
        if (
            repository is None
            or repository.key in seen
            or repository.key in (excluded_repositories or set())
            or repository_name in (excluded_project_names or set())
        ):
            continue
        repositories.append(repository)
        seen.add(repository.key)

    modules: list[Module] = []
    used_names: set[str] = set()
    for repository in repositories:
        module = client.find_module(repository)
        if module is None:
            continue

        # West project names must be unique.  Keep the repository name in the
        # common case and qualify only an actual collision.
        name = module.name
        if name.lower() in used_names:
            name = f"{repository.path.replace('/', '-')}-{name}"
            module = Module(name=name, url=module.url, revision=module.revision)
        used_names.add(name.lower())
        modules.append(module)
    return modules


def linked_repository_keys(markdown: str) -> set[tuple[str, str]]:
    """Return normalized repository keys linked from Markdown."""

    repositories: set[tuple[str, str]] = set()
    for link in extract_links(markdown):
        repository = normalize_repository(link)
        if repository is not None:
            repositories.add(repository.key)
    return repositories


def count_excluded_links(
    markdown: str,
    excluded_repositories: set[tuple[str, str]],
    excluded_project_names: set[str],
) -> int:
    """Count unique README repositories excluded by the official manifest."""

    count = 0
    seen: set[tuple[str, str]] = set()
    for link in extract_links(markdown):
        repository = normalize_repository(link)
        if repository is None or repository.key in seen:
            continue
        seen.add(repository.key)
        if (
            repository.key in excluded_repositories
            or repository.path.rsplit("/", 1)[-1].lower() in excluded_project_names
        ):
            count += 1
    return count


def yaml_string(value: str) -> str:
    """Quote a scalar safely for the small YAML subset used by west.yml."""

    return "'" + value.replace("'", "''") + "'"


def render_west_manifest(modules: Iterable[Module]) -> str:
    """Render modules as a valid, standalone west manifest."""

    lines = [
        "# Generated from README.md by generate_west_manifest.py.",
        "manifest:",
        "  projects:",
    ]
    modules = list(modules)
    if not modules:
        lines[-1] = "  projects: []"
    else:
        for module in modules:
            lines.extend(
                [
                    f"    - name: {yaml_string(module.name)}",
                    f"      url: {yaml_string(module.url)}",
                    f"      revision: {yaml_string(module.revision)}",
                ]
            )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find Zephyr modules linked in a Markdown file and emit "
            "external-modules.yml."
        )
    )
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument(
        "--output", type=Path, default=Path("external-modules.yml")
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token; defaults to GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--zephyr-manifest-url",
        default=DEFAULT_ZEPHYR_MANIFEST_URL,
        help="Official west manifest used to exclude existing Zephyr projects.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        markdown = args.readme.read_text(encoding="utf-8")
        client = ApiClient(args.github_token)
        excluded_repositories, excluded_project_names = client.load_west_manifest_exclusions(
            args.zephyr_manifest_url
        )
        excluded_linked_repositories = count_excluded_links(
            markdown, excluded_repositories, excluded_project_names
        )
        print(
            f"Loaded {len(excluded_repositories)} Zephyr west projects; "
            f"excluding {excluded_linked_repositories} linked repository(ies)."
        )
        modules = find_modules(
            markdown, client, excluded_repositories, excluded_project_names
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_west_manifest(modules), encoding="utf-8")
    except (OSError, RemoteCheckError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Generated {args.output} with {len(modules)} Zephyr module(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
