from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent
RESOURCES_DIR = PLUGIN_ROOT / "resources"

def get_resource(filename: str) -> str:
    return str(RESOURCES_DIR / filename)