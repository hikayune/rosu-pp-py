from __future__ import annotations

import email.message
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packaging.tags import Tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent
METADATA_VERSION = "2.1"


def _load_toml(path: Path) -> Dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    with path.open("rb") as file:
        return tomllib.load(file)


def _pyproject() -> Dict[str, Any]:
    return _load_toml(PROJECT_ROOT / "pyproject.toml")


def _project() -> Dict[str, Any]:
    return _pyproject().get("project", {})


def _package_name() -> str:
    return str(_project().get("name", "rosu-pp-py"))


def _dist_info_name() -> str:
    return canonicalize_name(_package_name()).replace("-", "_")


def _package_version() -> str:
    return str(_project().get("version", "0.0.0"))


def _release_tag() -> Optional[str]:
    tag = os.environ.get("ROSU_PP_PY_PREBUILT_TAG")
    return tag.strip() if tag else None


def _git_remote_url() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(PROJECT_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None


def _github_repo_from_url(url: str) -> Optional[str]:
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s#?]+)", url)
    if not match:
        return None

    owner = match.group(1)
    repo = re.sub(r"\.git$", "", match.group(2))
    return f"{owner}/{repo}"


def _configured_repo() -> Optional[str]:
    env_repo = os.environ.get("ROSU_PP_PY_PREBUILT_REPO")
    if env_repo:
        return env_repo.strip()

    git_remote = _git_remote_url()
    if git_remote:
        repo = _github_repo_from_url(git_remote)
        if repo:
            return repo

    config = _pyproject().get("tool", {}).get("rosu_pp_py_prebuilt", {})
    repo = str(config.get("repository", "")).strip()
    if repo:
        return repo

    urls = _project().get("urls", {})
    for key in ("Repository", "Source", "Homepage"):
        repo_url = urls.get(key)
        if repo_url:
            repo = _github_repo_from_url(str(repo_url))
            if repo:
                return repo

    return None


def _github_headers(download: bool = False) -> Dict[str, str]:
    headers = {
        "Accept": "application/octet-stream" if download else "application/vnd.github+json",
        "User-Agent": "rosu-pp-py-prebuilt-installer",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _read_json(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers=_github_headers())
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, destination: Path) -> Path:
    request = urllib.request.Request(url, headers=_github_headers(download=True))
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as file:
            shutil.copyfileobj(response, file)
    return destination


def _wheel_name_from_asset(asset_name: str) -> Optional[str]:
    if asset_name.endswith(".whl"):
        return asset_name
    if asset_name.endswith(".whl.zip"):
        return asset_name[:-4]
    return None


def _compatibility_score(wheel_name: str, supported_tags: List[Tag]) -> Optional[int]:
    try:
        name, version, _build, wheel_tags = parse_wheel_filename(wheel_name)
    except Exception:
        return None

    if name != canonicalize_name(_package_name()):
        return None

    if version != Version(_package_version()):
        return None

    wheel_tags = set(wheel_tags)
    for index, tag in enumerate(supported_tags):
        if tag in wheel_tags:
            return index

    return None


def _release(repo: str, tag: Optional[str]) -> Dict[str, Any]:
    if tag:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
    return _read_json(url)


def _release_assets(repo: str, tag: Optional[str]) -> Tuple[str, List[Dict[str, Any]]]:
    release = _release(repo, tag)
    release_tag = str(release.get("tag_name") or tag or "")
    return release_tag, list(release.get("assets", []))


def find_compatible_release_asset(repo: str, tag: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    supported_tags = list(sys_tags())
    candidates: List[Tuple[int, str, str]] = []
    release_tag, assets = _release_assets(repo, tag)

    for asset in assets:
        asset_name = str(asset.get("name", ""))
        wheel_name = _wheel_name_from_asset(asset_name)
        if not wheel_name:
            continue

        score = _compatibility_score(wheel_name, supported_tags)
        download_url = str(asset.get("browser_download_url") or "")
        if score is not None and download_url:
            candidates.append((score, asset_name, download_url))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _score, asset_name, download_url = candidates[0]
    return release_tag, asset_name, download_url


def _extract_wheel(asset_path: Path, output_dir: Path) -> Optional[str]:
    supported_tags = list(sys_tags())

    with zipfile.ZipFile(asset_path) as archive:
        candidates = []
        for member in archive.namelist():
            wheel_name = Path(member).name
            if not wheel_name.endswith(".whl"):
                continue

            score = _compatibility_score(wheel_name, supported_tags)
            if score is not None:
                candidates.append((score, member, wheel_name))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        _score, member, wheel_name = candidates[0]
        wheel_path = output_dir / wheel_name

        with archive.open(member) as source, wheel_path.open("wb") as target:
            shutil.copyfileobj(source, target)

    return wheel_path.name


def download_compatible_wheel(
    wheel_directory: str,
    repo: Optional[str] = None,
    tag: Optional[str] = None,
) -> Optional[str]:
    repo = repo or _configured_repo()
    tag = tag or _release_tag()
    if not repo:
        return None

    try:
        selected_asset = find_compatible_release_asset(repo, tag)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None

    if not selected_asset:
        return None

    _release_tag_name, asset_name, download_url = selected_asset
    output_dir = Path(wheel_directory)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        asset_path = _download(download_url, temp_path / asset_name)

        if asset_name.endswith(".whl"):
            wheel_path = output_dir / asset_name
            shutil.copy2(asset_path, wheel_path)
            return wheel_path.name

        return _extract_wheel(asset_path, output_dir)


def _maturin(function_name: str, *args: Any, **kwargs: Any) -> Any:
    import maturin

    return getattr(maturin, function_name)(*args, **kwargs)


def get_requires_for_build_wheel(config_settings: Optional[Dict[str, Any]] = None) -> List[str]:
    if os.environ.get("ROSU_PP_PY_FORCE_BUILD"):
        return _maturin("get_requires_for_build_wheel", config_settings)
    return []


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: Optional[Dict[str, Any]] = None,
) -> str:
    project = _project()
    dist_info = f"{_dist_info_name()}-{_package_version()}.dist-info"
    dist_info_dir = Path(metadata_directory) / dist_info
    dist_info_dir.mkdir(parents=True, exist_ok=True)

    metadata = email.message.Message()
    metadata["Metadata-Version"] = METADATA_VERSION
    metadata["Name"] = _package_name()
    metadata["Version"] = _package_version()

    if project.get("description"):
        metadata["Summary"] = str(project["description"])
    if project.get("requires-python"):
        metadata["Requires-Python"] = str(project["requires-python"])

    for classifier in project.get("classifiers", []):
        metadata["Classifier"] = str(classifier)

    for keyword in project.get("keywords", []):
        metadata["Keywords"] = str(keyword)

    authors = project.get("authors") or []
    if isinstance(authors, list):
        names = [author.get("name") for author in authors if isinstance(author, dict) and author.get("name")]
        emails = [author.get("email") for author in authors if isinstance(author, dict) and author.get("email")]
        if names:
            metadata["Author"] = ", ".join(names)
        if emails:
            metadata["Author-email"] = ", ".join(emails)

    (dist_info_dir / "METADATA").write_text(metadata.as_string(), encoding="utf-8")
    (dist_info_dir / "WHEEL").write_text(
        "Wheel-Version: 1.0\n"
        "Generator: rosu-pp-py-prebuilt-backend\n"
        "Root-Is-Purelib: false\n"
        "Tag: py3-none-any\n",
        encoding="utf-8",
    )
    return dist_info


def build_wheel(
    wheel_directory: str,
    config_settings: Optional[Dict[str, Any]] = None,
    metadata_directory: Optional[str] = None,
) -> str:
    if not os.environ.get("ROSU_PP_PY_FORCE_BUILD"):
        wheel = download_compatible_wheel(wheel_directory)
        if wheel:
            return wheel

    if os.environ.get("ROSU_PP_PY_ONLY_PREBUILT"):
        repo = _configured_repo() or "repositório não detectado"
        tag = _release_tag() or "última release"
        raise RuntimeError(
            f"Nenhuma wheel compatível foi encontrada em {repo} na {tag}."
        )

    return _maturin("build_wheel", wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory: str, config_settings: Optional[Dict[str, Any]] = None) -> str:
    return _maturin("build_sdist", sdist_directory, config_settings)
