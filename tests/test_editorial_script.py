"""Sprint 4B-3 structured script-generation workflow tests."""

from dailycast.llm.editorial_service import AIEditorialService


def test_editorial_service_exposes_structured_script_generation() -> None:
    """The editorial service owns the documented script-generation operation."""
    service = AIEditorialService(None, None)  # type: ignore[arg-type]

    assert callable(getattr(service, "generate_script", None))
