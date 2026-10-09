from pathlib import Path


def app_version(request):
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    return {"APP_VERSION": version}