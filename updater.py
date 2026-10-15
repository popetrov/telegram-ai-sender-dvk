"""Read-only GitHub Releases checker for Telegram AI Sender V2.0.

This module only checks for published releases.
It does not download, install, or modify project files.
"""

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from pathlib import Path


REPOSITORY = "popetrov/telegram-ai-sender-dvk"
RELEASES_FEED_URL = (
    f"https://github.com/{REPOSITORY}/releases.atom"
)

VERSION_FILE = "version.txt"


@dataclass
class ReleaseInfo:
    tag: str
    title: str
    url: str


def normalize_version(value: str) -> tuple[int, ...] | None:
    """Convert a version such as v1.2.3 into a numeric tuple."""
    value = value.strip()

    if value.startswith(("v", "V")):
        value = value[1:]

    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        return None

    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def get_current_version() -> str:
    """Read and validate the local version.txt file."""
    version_path = Path(__file__).resolve().parent / VERSION_FILE

    if not version_path.is_file():
        raise RuntimeError(
            f"Version file not found: {version_path}"
        )

    version = version_path.read_text(encoding="utf-8").strip()

    if normalize_version(version) is None:
        raise RuntimeError(
            f"Invalid version in {version_path}: {version!r}"
        )

    return version


def get_latest_release(timeout: int = 10) -> ReleaseInfo | None:
    """Return the latest published release, or None if there are none."""

    request = urllib.request.Request(
        RELEASES_FEED_URL,
        headers={
            "User-Agent": "Telegram-AI-Sender-Updater/1.0",
            "Accept": "application/atom+xml, application/xml",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            xml_data = response.read(2_000_000)

    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHub returned HTTP {exc.code} while checking releases."
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not connect to GitHub: {exc.reason}"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "Connection to GitHub timed out."
        ) from exc

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise RuntimeError(
            "GitHub returned an unreadable releases feed."
        ) from exc

    namespace = {
        "atom": "http://www.w3.org/2005/Atom"
    }

    entry = root.find("atom:entry", namespace)

    if entry is None:
        return None

    title_element = entry.find("atom:title", namespace)
    id_element = entry.find("atom:id", namespace)
    link_element = entry.find("atom:link", namespace)

    title = (
        unescape(title_element.text.strip())
        if title_element is not None and title_element.text
        else ""
    )

    release_id = (
        id_element.text.strip()
        if id_element is not None and id_element.text
        else ""
    )

    url = (
        link_element.attrib.get("href", "")
        if link_element is not None
        else ""
    )

    # Typical release feed IDs end with /tag/v1.2.3.
    tag_match = re.search(r"/tag/([^/\s]+)$", release_id)

    if not tag_match:
        raise RuntimeError(
            "Could not determine the release tag from GitHub's feed."
        )

    tag = tag_match.group(1)

    if not url:
        url = (
            f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
        )

    return ReleaseInfo(
        tag=tag,
        title=title,
        url=url,
    )


def check_for_update() -> None:
    """Check the latest release without changing anything."""

    print("TELEGRAM AI SENDER — UPDATE CHECK")
    print(f"Repository: {REPOSITORY}")

    try:
        current_version_text = get_current_version()
    except (RuntimeError, OSError) as exc:
        print(f"Could not read current version: {exc}")
        return

    print(f"Current version: {current_version_text}")
    print()

    try:
        release = get_latest_release()
    except RuntimeError as exc:
        print(f"Could not check for updates: {exc}")
        return

    if release is None:
        print("No published releases found.")
        print("No files have been changed.")
        return

    latest_version = normalize_version(release.tag)
    current_version = normalize_version(current_version_text)

    print(f"Latest release: {release.tag}")
    print(f"Release title: {release.title}")
    print(f"Release page: {release.url}")
    print()

    if latest_version is None or current_version is None:
        print(
            "The versions cannot be compared automatically. "
            "No files have been changed."
        )
        return

    # Pad version tuples so 1.2 and 1.2.0 compare equally.
    size = max(len(latest_version), len(current_version))
    latest_version += (0,) * (size - len(latest_version))
    current_version += (0,) * (size - len(current_version))

    if latest_version > current_version:
        print("A newer release is available.")
        print("This checker does not install updates.")
    elif latest_version == current_version:
        print("The current version is up to date.")
    else:
        print(
            "The published release is older than the configured version."
        )

    print("No project files have been changed.")


if __name__ == "__main__":
    check_for_update()