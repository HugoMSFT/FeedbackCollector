import os
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class InlineScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        script_type = attributes.get("type", "").lower()
        if attributes.get("src") or script_type not in {
            "",
            "text/javascript",
            "application/javascript",
            "module",
        }:
            return
        self._current = []

    def handle_data(self, data):
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._current is not None:
            self.scripts.append("".join(self._current))
            self._current = None


def check_file(path: Path, label: str) -> None:
    result = subprocess.run(
        ["node", "--check", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"JavaScript syntax error in {label}:\n{details}")


def main() -> None:
    for path in sorted((SRC_DIR / "static" / "js").glob("*.js")):
        check_file(path, str(path.relative_to(ROOT)))

    os.environ.setdefault("FLASK_SECRET_KEY", "javascript-syntax-check")
    import app as app_module

    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    with tempfile.TemporaryDirectory() as temp_dir:
        for route in ("/", "/feedback", "/insights"):
            response = client.get(route)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Unable to render {route}: HTTP {response.status_code}"
                )

            parser = InlineScriptParser()
            parser.feed(response.get_data(as_text=True))
            for index, source in enumerate(parser.scripts, start=1):
                path = Path(temp_dir) / f"inline-{index}.js"
                path.write_text(source, encoding="utf-8")
                check_file(path, f"{route} inline script {index}")

    print("JavaScript syntax checks passed")


if __name__ == "__main__":
    main()
