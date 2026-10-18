from app.core.retry import (
    NonRetryableError,
    RetryPolicy,
    RetryableError,
    compute_delay,
    is_retryable,
)


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(initial_delay_s=1, multiplier=2, max_delay_s=5, jitter=0)
    assert compute_delay(1, policy) == 1
    assert compute_delay(2, policy) == 2
    assert compute_delay(3, policy) == 4
    assert compute_delay(10, policy) == 5


def test_jitter_stays_in_range():
    policy = RetryPolicy(initial_delay_s=10, jitter=0.2, multiplier=1, max_delay_s=100)
    for _ in range(20):
        delay = compute_delay(1, policy)
        assert 8 <= delay <= 12


def test_retryable_classification():
    policy = RetryPolicy()
    assert is_retryable(RetryableError("timeout"), policy)
    assert not is_retryable(NonRetryableError("bad args"), policy)
    assert not is_retryable(ValueError("bug"), policy)
