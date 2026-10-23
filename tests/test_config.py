from lia.config import get_settings


def test_settings_defaults(monkeypatch):
    for k in ("LIARA_API_BASE", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOX_API_TOKEN", "box-token")
    s = get_settings()
    assert s.liara_api_base == "https://api.liara.ir"
    assert s.llm_base_url == "https://ai.boxapi.ir/ai/v1"
    assert s.llm_api_key == "box-token"
    assert s.llm_model == "gpt-5.4-mini"


def test_paths_default_to_repo(monkeypatch):
    monkeypatch.delenv("LIA_PROJECTS_DIR", raising=False)
    s = get_settings()
    assert s.projects_dir.name == "demo" and (s.projects_dir / "shop").is_dir()
    assert s.state_dir.name == ".lia"
