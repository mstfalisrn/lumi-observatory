# LUMI — security redaction, DLP, and environment-secret tests
from observability.security import (
    Redactor,
    contains_secret,
    load_secrets_from_env,
    redact,
    scrub_and_flag,
)


def test_redact_tg_token():
    token = "123456789:" + ("T" * 35)
    out = redact(f"token: {token}")
    assert token not in out
    assert "REDACT" in out


def test_redact_password_assignment():
    password = "P" * 24
    out = redact(f"config: password={password}")
    assert password not in out


def test_redact_aws_key():
    key = "AKIA" + ("A" * 16)
    out = redact(f"key {key}")
    assert key not in out


def test_redact_empty_and_none():
    assert redact("") == ""
    assert redact("clean text") == "clean text"


def test_contains_secret():
    token = "123456789:" + ("T" * 35)
    assert contains_secret(f"token: {token}")
    assert not contains_secret("an ordinary text")


def test_scrub_and_flag():
    password = "P" * 20
    text = f"password={password}"
    scrubbed, had = scrub_and_flag(text)
    assert had is True
    assert password not in scrubbed
    clean, clean_had = scrub_and_flag("hello world")
    assert clean_had is False
    assert clean == "hello world"


def test_load_secrets_from_env_redacts_literal():
    key = "sk-" + ("K" * 32)
    env = {"LLM_API_KEY": key, "UNRELATED": "hello world"}
    added = load_secrets_from_env(environ=env)
    assert added >= 1
    assert key not in redact(f"key {key} is present")


def test_load_secrets_from_env_skips():
    env = {
        "LLM_API_KEY": "CHANGE_ME",
        "SHORT": "short",
        "PATH_LIKE": "/usr/bin/python",
        "USERNAME": "this-is-a-long-value-but-the-key-name-is-not-hidden",
    }
    assert load_secrets_from_env(environ=env) == 0


def test_redactor_class():
    sensitive_value = "S" * 20
    redactor = Redactor()
    redactor.add_literal(sensitive_value)
    out = redactor.scrub(f"value {sensitive_value} is present")
    assert sensitive_value not in out
    assert redactor.contains_secret(sensitive_value) is True
    scrubbed, had = redactor.scrub_and_flag(f"x {sensitive_value} y")
    assert had is True and sensitive_value not in scrubbed
