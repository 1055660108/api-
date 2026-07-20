from __future__ import annotations


SUPPORTED_PLATFORMS = {"dola", "doubao", "qianwen"}
PLATFORM_LABELS = {
    "dola": "Dola",
    "doubao": "豆包",
    "qianwen": "千问",
}
DEFAULT_PLATFORM = "dola"
DEFAULT_MODELS = {
    "dola": ["Seedance 2.0"],
    "doubao": ["Seedance 2.0 Mini", "Seedance 2.0 Fast"],
    "qianwen": ["万相 2.7"],
}


def normalize_platform(value: str | None) -> str:
    platform = str(value or DEFAULT_PLATFORM).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("invalid platform")
    return platform


def normalize_model(value: str | None) -> str:
    return str(value or "").strip()[:80]
