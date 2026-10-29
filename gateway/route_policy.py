"""Node health scoring, switch hysteresis and recovery backoff.

The selector used to classify a route with a single hard throughput floor.
That made every node below the floor look equally bad, so a working but slow
node was never replaced by a significantly faster one. This module keeps the
old ``healthy``/``low``/``blocked``/``disconnected`` labels for compatibility
but derives them from a small score. Callers can therefore compare a candidate
with the active route instead of only asking "is it healthy?".
"""
from dataclasses import dataclass
import statistics


@dataclass(frozen=True)
class RoutePolicy:
    minimum_effective_kbps: float = 15.0
    minimum_body_kbps: float = 30.0
    maximum_first_byte_ms: float = 3000.0
    healthy_score: float = 0.43
    switch_score_margin: float = 0.08
    switch_ratio: float = 1.15
    sticky_seconds: float = 600.0
    recovery_backoff_seconds: tuple = (60, 120, 300, 600, 1200, 1800)
    target_effective_kbps: float = 80.0
    target_body_kbps: float = 160.0


DEFAULT_POLICY = RoutePolicy()


def _number(value, default=0.0):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _backoff_sequence(base, maximum):
    base = max(1.0, float(base))
    maximum = max(base, float(maximum))
    values = []
    value = base
    while value < maximum and len(values) < 8:
        values.append(value)
        value *= 2
    values.append(maximum)
    return tuple(dict.fromkeys(values))


def policy_from_settings(settings):
    """Build a policy from flat settings while keeping the old key usable."""
    legacy = settings.get('SakuraMinimumThroughputKBps')
    minimum_effective = settings.get('SakuraMinimumEffectiveKBps')
    if minimum_effective is None:
        minimum_effective = legacy if legacy is not None else 15
    try:
        minimum_body = float(settings.get('SakuraMinimumBodyKBps', 30))
    except (TypeError, ValueError):
        minimum_body = 30.0
    try:
        maximum_first = float(settings.get('SakuraMaxFirstByteMs', 3000))
    except (TypeError, ValueError):
        maximum_first = 3000.0
    try:
        healthy_score = float(settings.get('SakuraHealthyScore', 0.43))
    except (TypeError, ValueError):
        healthy_score = 0.43
    try:
        score_margin = float(settings.get('SakuraSwitchScoreMargin', 0.08))
    except (TypeError, ValueError):
        score_margin = 0.08
    try:
        switch_ratio = float(settings.get('SakuraSwitchRatio', 1.15))
    except (TypeError, ValueError):
        switch_ratio = 1.15
    try:
        sticky = float(settings.get('SakuraStickySeconds', 600))
    except (TypeError, ValueError):
        sticky = 600.0
    try:
        backoff_base = float(settings.get('SakuraRecoveryBackoffSeconds', 60))
    except (TypeError, ValueError):
        backoff_base = 60.0
    try:
        backoff_max = float(settings.get('SakuraRecoveryMaxSeconds', 1800))
    except (TypeError, ValueError):
        backoff_max = 1800.0
    return RoutePolicy(
        minimum_effective_kbps=_number(minimum_effective, 15),
        minimum_body_kbps=_number(minimum_body, 0),
        maximum_first_byte_ms=_number(maximum_first, 0),
        healthy_score=min(1.0, _number(healthy_score, 0.43)),
        switch_score_margin=_number(score_margin, 0.08),
        switch_ratio=max(1.0, _number(switch_ratio, 1.15)),
        sticky_seconds=_number(sticky, 600),
        recovery_backoff_seconds=_backoff_sequence(backoff_base, backoff_max),
    )


def assess(result, policy=DEFAULT_POLICY):
    """Return health, score and the measurements behind the decision."""
    health_hint = result.get('health')
    if 'ok' in result:
        ok = bool(result.get('ok'))
    else:
        # Older callers stored only the health label. Treat a non-disconnected
        # label as a connected sample instead of dropping it as a failure.
        ok = health_hint != 'disconnected' if health_hint else False
    complete = bool(result.get('complete', ok))
    chatgpt_ok = result.get('chatgpt_ok')
    if chatgpt_ok is None and health_hint == 'blocked':
        chatgpt_ok = False
    effective = result.get('effective_throughput_kbps')
    if effective is None:
        effective = result.get('throughput_kbps', 0)
    effective = _number(effective)
    body = _number(result.get('throughput_kbps', 0))
    first = _number(result.get('first_byte_ms', 0))
    stability = min(1.0, _number(result.get('stability', 1.0), 1.0))

    reasons = []
    if chatgpt_ok is False:
        reasons.append('chatgpt_blocked')
        return {'health': 'blocked', 'score': 0.0,
                'effective_kbps': effective, 'body_kbps': body,
                'first_byte_ms': first, 'reasons': reasons}
    if not ok or not complete:
        reasons.append('disconnected')
        return {'health': 'disconnected', 'score': 0.0,
                'effective_kbps': effective, 'body_kbps': body,
                'first_byte_ms': first, 'reasons': reasons}

    effective_score = min(1.0, effective / max(.001, policy.target_effective_kbps))
    body_score = min(1.0, body / max(.001, policy.target_body_kbps))
    if first <= 0 or policy.maximum_first_byte_ms <= 0:
        first_score = 1.0
    else:
        first_score = max(0.0, 1.0 - first / policy.maximum_first_byte_ms)
    score = (0.45 * effective_score + 0.30 * body_score
             + 0.15 * first_score + 0.10 * stability)

    if effective < policy.minimum_effective_kbps:
        reasons.append('effective_below_floor')
    if policy.minimum_body_kbps > 0 and body < policy.minimum_body_kbps:
        reasons.append('body_below_floor')
    if policy.maximum_first_byte_ms > 0 and first > policy.maximum_first_byte_ms:
        reasons.append('first_byte_above_limit')
    if score < policy.healthy_score:
        reasons.append('score_below_healthy')
    health = 'low' if reasons else 'healthy'
    if not reasons:
        reasons.append('healthy')
    return {'health': health, 'score': round(score, 4),
            'effective_kbps': round(effective, 3), 'body_kbps': round(body, 3),
            'first_byte_ms': int(first), 'reasons': reasons}


def is_meaningfully_better(candidate, baseline, policy=DEFAULT_POLICY):
    """Whether replacing ``baseline`` with ``candidate`` is justified.

    A low route can replace another low route when it is significantly faster.
    A healthy route needs a larger score or speed advantage, which keeps the
    selector from moving between two similarly good nodes.
    """
    candidate = assess(candidate, policy)
    baseline = assess(baseline, policy)
    if candidate['health'] in ('blocked', 'disconnected'):
        return False
    if baseline['health'] in ('blocked', 'disconnected'):
        return True
    if baseline['health'] == 'low':
        score_gain = (candidate['score'] - baseline['score']
                      >= policy.switch_score_margin)
        speed_gain = (candidate['effective_kbps']
                      >= baseline['effective_kbps'] * 1.10)
        recovered = (candidate['health'] == 'healthy'
                     and baseline['score'] < policy.healthy_score)
        return (score_gain and speed_gain) or recovered
    return candidate['health'] == 'healthy' and (
        candidate['score'] >= baseline['score'] + policy.switch_score_margin
        or candidate['effective_kbps']
        >= baseline['effective_kbps'] * policy.switch_ratio)


def pending_decision(baseline, samples, current, policy=DEFAULT_POLICY):
    """Decide whether a switched route should be kept after observation.

    ``samples`` contains the observed probe results including ``current``.
    The comparison is relative to the baseline; there is no absolute speed
    floor here, so a low but much faster route can be accepted.
    """
    current_assessment = assess(current, policy)
    baseline_assessment = assess(baseline, policy)
    if current_assessment['health'] == 'disconnected':
        return False, 'switched node disconnected'
    if current_assessment['health'] == 'blocked':
        return False, 'switched node did not restore ChatGPT service'
    if baseline_assessment['health'] in ('blocked', 'disconnected'):
        return True, 'service restored'
    measured = [assess(item, policy) for item in samples if item]
    if not measured:
        measured = [current_assessment]
    median_score = statistics.median(item['score'] for item in measured)
    median_effective = statistics.median(
        item['effective_kbps'] for item in measured)
    improved = (
        median_score >= baseline_assessment['score'] + 0.05
        and median_effective >= baseline_assessment['effective_kbps'] * 0.90)
    if not improved and median_score >= policy.healthy_score:
        improved = baseline_assessment['score'] < policy.healthy_score
    return (True, 'throughput improved') if improved else (
        False, 'no meaningful improvement')


def backoff_seconds(failures, policy=DEFAULT_POLICY):
    """Exponential recovery backoff, capped by the policy sequence."""
    failures = max(1, int(failures or 1))
    sequence = policy.recovery_backoff_seconds or (60,)
    return float(sequence[min(failures - 1, len(sequence) - 1)])


def classify_health(result, minimum_throughput_kbps):
    """Compatibility wrapper for the old single-threshold API."""
    policy = RoutePolicy(
        minimum_effective_kbps=max(0.0, _number(minimum_throughput_kbps, 0)),
        minimum_body_kbps=0.0,
        maximum_first_byte_ms=0.0,
        healthy_score=0.43,
    )
    return assess(result, policy)['health']
