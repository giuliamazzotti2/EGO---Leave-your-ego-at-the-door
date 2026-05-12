import numpy as np


def bpm_ascending(tracks):
    """Sort tracks by tempo (BPM) from lowest to highest."""
    return sorted(tracks, key=lambda t: t.get("tempo", 0))


def energy_ascending(tracks):
    """Sort tracks by energy from lowest to highest."""
    return sorted(tracks, key=lambda t: t.get("energy", 0))


def peak_and_drop(tracks):
    """
    DJ arc: build to a peak then wind down.

    Splits into three energy bands and sequences them:
    low → mid → high → mid → low
    Within each band tracks are sorted by BPM ascending for smooth transitions.
    """
    low, mid, high = [], [], []
    for t in tracks:
        e = t.get("energy", 0)
        if e < 0.4:
            low.append(t)
        elif e <= 0.7:
            mid.append(t)
        else:
            high.append(t)

    def by_bpm(lst):
        return sorted(lst, key=lambda t: t.get("tempo", 0))

    # Split mid band in two halves: ascending into peak, descending out
    mid_sorted = by_bpm(mid)
    mid_up = mid_sorted[: len(mid_sorted) // 2]
    mid_down = list(reversed(mid_sorted[len(mid_sorted) // 2 :]))

    return by_bpm(low) + mid_up + by_bpm(high) + mid_down + list(reversed(by_bpm(low)))


def valence_ascending(tracks):
    """Sort tracks by valence from darkest/saddest to most euphoric."""
    return sorted(tracks, key=lambda t: t.get("valence", 0))


def smart_mix(tracks):
    """
    Greedy nearest-neighbour sequencing for smooth flow.

    Each track gets a combined score:
        score = (energy * 0.5) + (tempo_normalized * 0.5)

    Starting from the track with the lowest score, at each step the algorithm
    picks the unvisited track whose score is closest to the current one,
    minimising abrupt jumps in energy and tempo.
    """
    if not tracks:
        return []

    tempos = np.array([t.get("tempo", 0) for t in tracks], dtype=float)
    t_min, t_max = tempos.min(), tempos.max()
    t_range = t_max - t_min if t_max != t_min else 1.0
    tempo_norm = (tempos - t_min) / t_range

    energies = np.array([t.get("energy", 0) for t in tracks], dtype=float)
    scores = energies * 0.5 + tempo_norm * 0.5

    remaining = list(range(len(tracks)))
    # Start from the track with the lowest combined score
    current = int(np.argmin(scores))
    remaining.remove(current)
    order = [current]

    while remaining:
        current_score = scores[current]
        nearest = min(remaining, key=lambda i: abs(scores[i] - current_score))
        order.append(nearest)
        remaining.remove(nearest)
        current = nearest

    return [tracks[i] for i in order]


ALGORITHMS = {
    "bpm_ascending": bpm_ascending,
    "energy_ascending": energy_ascending,
    "peak_and_drop": peak_and_drop,
    "valence_ascending": valence_ascending,
    "smart_mix": smart_mix,
}


def apply(algorithm_key, tracks):
    """
    Dispatch to the requested algorithm.

    Raises ValueError for unknown keys so the caller can return a 400.
    """
    fn = ALGORITHMS.get(algorithm_key)
    if fn is None:
        raise ValueError(f"Unknown algorithm: {algorithm_key!r}")
    return fn(tracks)
