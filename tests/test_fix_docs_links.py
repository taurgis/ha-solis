"""Documentation links: repair anchors and relative Markdown links resolve.

Repairs link to ``docs/troubleshooting.md#<anchor>``. GitHub and VitePress
both build heading anchors from the heading text, so a renamed heading would
silently break the link a user clicks in the Repairs panel.
"""

from __future__ import annotations

from pathlib import Path
import re

from custom_components.solis.coordinator import LEARN_MORE

ROOT = Path(__file__).resolve().parents[1]
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"```.*?```", re.DOTALL)


def _slug(text: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    body = _FENCE.sub("", path.read_text(encoding="utf-8"))
    return {_slug(match) for match in _HEADING.findall(body)}


def _markdown_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md"]
    files += sorted((ROOT / "docs").rglob("*.md"))
    files += sorted((ROOT / "specs").glob("*.md"))
    return [path for path in files if "node_modules" not in path.parts]


def test_slug_matches_github() -> None:
    """The anchors used below follow GitHub's rule."""
    assert _slug("Which connection should I pick?") == "which-connection-should-i-pick"
    assert _slug("S5 port closed") == "s5-port-closed"
    assert _slug("Migrating from hultenvp/solis-sensor") == "migrating-from-hultenvpsolis-sensor"


def test_repair_learn_more_anchors_exist() -> None:
    """Every learn_more_url points at a heading in troubleshooting.md."""
    anchors = _anchors(ROOT / "docs" / "troubleshooting.md")
    expected = {"logger-gateway", "wrong-protocol", "s5-port-closed", "legacy-control"}
    used = {url.split("#", 1)[1] for url in LEARN_MORE.values() if url}
    assert used == expected
    assert expected <= anchors


def test_relative_markdown_links_resolve() -> None:
    """Relative links in the docs, specs and README point at real files and headings."""
    broken: list[str] = []
    for path in _markdown_files():
        body = _FENCE.sub("", path.read_text(encoding="utf-8"))
        for target in _LINK.findall(body):
            if re.match(r"^[a-z]+:", target) or target.startswith("/"):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = path if not file_part else (path.parent / file_part).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)}: {target}")
                continue
            if anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                broken.append(f"{path.relative_to(ROOT)}: {target} (no heading)")
    assert broken == []
