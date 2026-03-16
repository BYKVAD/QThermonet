from pathlib import Path

PROCESSING_DIR = Path(__file__).parent
PLUGIN_ROOT = Path(PROCESSING_DIR).parent
RESOURCES_DIR = PLUGIN_ROOT / "resources"

def get_resource(filename: str) -> str:
    return str(RESOURCES_DIR / filename)