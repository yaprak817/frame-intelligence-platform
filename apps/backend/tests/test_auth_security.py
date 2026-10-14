from app.security.auth import (
    generate_session_token,
    hash_password,
    hash_session_token,
    password_needs_rehash,
    verify_password,
)


def test_password_hash_round_trip_and_wrong_password_rejected():
    password = "Correct-Horse-123!"
    password_hash = hash_password(password)

    assert password_hash != password
    assert password_hash.startswith("$argon2")
    assert verify_password(password, password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_invalid_password_hash_is_rejected_safely():
    assert not verify_password("password", "not-an-argon2-hash")
    assert password_needs_rehash("not-an-argon2-hash")


def test_session_tokens_are_random_and_only_hash_is_deterministic():
    first = generate_session_token()
    second = generate_session_token()

    assert first
    assert second
    assert first != second
    assert hash_session_token(first) == hash_session_token(first)
    assert hash_session_token(first) != hash_session_token(second)
    assert len(hash_session_token(first)) == 64
