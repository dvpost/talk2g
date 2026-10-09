"""Check local inline Markdown links in root documents, docs/ and TODO/, without network access."""

import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def check_links(root: Path) -> list[str]:
    errors = []
    documents = sorted({*root.glob("*.md"), *(root / "docs").rglob("*.md"), *(root / "TODO").rglob("*.md")})
    for document in documents:
        fence = ""
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                if not fence:
                    fence = marker
                elif fence == marker:
                    fence = ""
                continue
            if fence:
                continue
            line = re.sub(r"`+[^`]*`+", "", line)
            for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", line):
                target = match[1].strip()
                target = target[1:].split(">", 1)[0] if target.startswith("<") else target.split()[0]
                address = urlsplit(target)
                if address.scheme or address.netloc or not address.path:
                    continue
                path = document.parent / unquote(address.path)
                if not path.exists():
                    errors.append(f"{document.relative_to(root)}:{number}: отсутствует {target}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    root = parser.parse_args().root.resolve()
    errors = check_links(root)
    for error in errors:
        print(error)
    if errors:
        return 1
    print("Локальные ссылки документации проверены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
