"""`val-*` teslim doğrulama hattının testleri (blockrewards kalibrasyon hattı).

Notlar canlıdan alınan gerçek şemayı taklit eder: TASK / REFERENCE ANSWER /
DELIVERABLE üçlüsü + "PASS or FAIL" talebi. Referans cevap oda genel olduğu
için yanıtta asla alıntılanmaz — bu testler o kuralı da doğrular.
"""

from apps.scheduler.tclk_solver import solve, solve_validation

EXACT = (
    "validation | Validate a deliverable. TASK that was posted: \"Count the rows.\" "
    "REFERENCE ANSWER the task's author holds (private to you as validator): "
    "\"offers 4, locks 2\". DELIVERABLE submitted by a worker: \"offers 4, locks 2\". "
    "Does the deliverable give the reference answer? Reply PASS or FAIL, then one sentence."
)

MISMATCH = (
    "validation | Validate a deliverable. TASK that was posted: \"Count the rows.\" "
    "REFERENCE ANSWER the task's author holds (private to you as validator): "
    "\"offers 4, locks 2\". DELIVERABLE submitted by a worker: \"offers 7, locks 9\". "
    "Reply PASS or FAIL, then one sentence."
)

CONTAINED = (
    "validation | Validate a deliverable. TASK that was posted: \"Fold the transcript.\" "
    "REFERENCE ANSWER the task's author holds (private to you as validator): \"accepted\". "
    "DELIVERABLE submitted by a worker: \"The contract's final status is accepted.\" "
    "Reply PASS or FAIL, then one sentence."
)


def test_exact_match_is_a_pass():
    assert solve_validation(EXACT) == "PASS. The deliverable states exactly the reference answer."


def test_verdict_reaches_the_answer_without_quoting_the_reference():
    answer = solve_validation(MISMATCH)
    assert answer is not None and answer.startswith("FAIL.")
    # referans cevap doğrulayıcıya özel: yanıt onu alıntılamamalı
    assert "offers 4" not in answer
    assert answer.count(".") == 2  # verdict + tek cümle


def test_contained_answer_is_a_pass():
    assert solve_validation(CONTAINED) == (
        "PASS. The deliverable states the same result as the reference answer."
    )


def test_solve_routes_validation_before_the_other_handlers():
    assert solve(MISMATCH) == solve_validation(MISMATCH)


def test_lowercase_deliverable_in_the_task_text_is_not_the_field():
    # "Validate a deliverable." görev metnindeki küçük harfli sözcük alan sanılmamalı
    brief = (
        "validation | Validate a deliverable. TASK that was posted: \"x\". "
        "REFERENCE ANSWER the task's author holds: \"7\". "
        "DELIVERABLE submitted by a worker: \"7\"."
    )
    assert solve_validation(brief) == "PASS. The deliverable states exactly the reference answer."


def test_field_order_is_respected():
    # DELIVERABLE, REFERENCE ANSWER'dan önce geliyorsa alan eşleşmesi güvenilmez
    brief = (
        "validation DELIVERABLE submitted by a worker: \"7\". "
        "REFERENCE ANSWER the task's author holds: \"8\"."
    )
    assert solve_validation(brief) is None


def test_unrelated_brief_stays_silent():
    assert solve_validation("review From https://example.com/spec.md: What prefix?") is None
    assert solve_validation("") is None
