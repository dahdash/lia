"""Settings loaded from the environment / local .env (never tracked)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    liara_api_key: str
    liara_api_base: str
    liara_team_id: str | None
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str
    projects_dir: Path  # local projects Lia may deploy
    state_dir: Path  # local deploy / env-change logs (gitignored)


def get_settings() -> Settings:
    return Settings(
        liara_api_key=os.environ.get("LIARA_API_KEY", ""),
        liara_api_base=os.environ.get("LIARA_API_BASE", "https://api.liara.ir").rstrip("/"),
        # resources and balance live in the team, selected per request with ?teamID=
        liara_team_id=os.environ.get("LIARA_TEAM_ID") or None,
        # boxapi is the default provider: one token, fixed base URL
        llm_base_url=os.environ.get("LLM_BASE_URL") or "https://ai.boxapi.ir/ai/v1",
        llm_api_key=os.environ.get("LLM_API_KEY") or os.environ.get("BOX_API_TOKEN") or None,
        llm_model=os.environ.get("LLM_MODEL", "gpt-5.4-mini"),
        projects_dir=Path(os.environ.get("LIA_PROJECTS_DIR") or ROOT / "demo"),
        state_dir=ROOT / ".lia",
    )
