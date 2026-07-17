#!/usr/bin/env python3
"""Generate a west manifest from repositories linked in a Markdown file."""

from __future__ import annotations

import argparse
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


def find_modules(markdown: str, client: ApiClient) -> list[Module]:
    """Check supported repositories linked in Markdown, preserving link order."""

    repositories: list[Repository] = []
    seen: set[tuple[str, str]] = set()
    for link in extract_links(markdown):
        repository = normalize_repository(link)
        if repository is None or repository.key in seen:
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
        description="Find Zephyr modules linked in a Markdown file and emit west.yml."
    )
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--output", type=Path, default=Path("west.yml"))
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token; defaults to GITHUB_TOKEN.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        markdown = args.readme.read_text(encoding="utf-8")
        modules = find_modules(markdown, ApiClient(args.github_token))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_west_manifest(modules), encoding="utf-8")
    except (OSError, RemoteCheckError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Generated {args.output} with {len(modules)} Zephyr module(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
