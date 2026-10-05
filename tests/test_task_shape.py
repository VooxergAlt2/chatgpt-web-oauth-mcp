from __future__ import annotations

from chatgpt_web_oauth_mcp.routing.task_shape import classify_task


def test_classify_task_is_provider_independent_and_deterministic() -> None:
    shape = classify_task(
        task="Investigate why production hourly runtime fails",
        kind="explore",
        complexity="medium",
        files_in_scope=["a.py", "b.py"],
        acceptance_criteria=["root cause with evidence"],
    )

    assert shape.purpose == "investigation"
    assert shape.breadth == "small"
    assert shape.depth == "high"
    assert shape.impact == "high"
    assert shape.verification_level == "concrete"
    assert shape.risk_class == "runtime"
    assert shape.confidence == 0.65
    payload = shape.to_dict()
    assert "harness" not in payload
    assert "model" not in payload
    assert shape.fingerprint() == classify_task(
        task="Investigate why production hourly runtime fails",
        kind="explore",
        complexity="medium",
        files_in_scope=["a.py", "b.py"],
        acceptance_criteria=["root cause with evidence"],
    ).fingerprint()
    assert len(shape.fingerprint()) == 16


def test_classify_task_uses_scope_without_provider_selection() -> None:
    shape = classify_task(
        task="Implement bounded change",
        kind="code",
        complexity="low",
        files_in_scope=[f"file-{index}.py" for index in range(6)],
    )

    assert shape.purpose == "implementation"
    assert shape.breadth == "medium"
    assert shape.depth == "low"
    assert shape.impact == "medium"


def test_classify_task_handles_russian_investigation_signals() -> None:
    shape = classify_task(
        task="Проведи аудит и найди причину падения в боевом рантайме",
        kind="explore",
        complexity="medium",
    )

    assert shape.purpose == "investigation"
    assert shape.depth == "high"
    assert shape.impact == "high"
    assert shape.risk_class == "runtime"
