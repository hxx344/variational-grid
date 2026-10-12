"""Dashboard sampling remains equivalent while scanning each bucket once."""
from copy import deepcopy
import unittest

from variational_grid.dashboard import reduce_points


def original_reduce_points(points, limit=900):
    """Frozen pre-optimization sampler used as a compatibility oracle."""
    if len(points) <= limit:
        return points
    dimensions = len(points[0]["pnl"]) + 2
    buckets = max(1, (limit - 2) // (2 * dimensions + 2))
    selected = {0, len(points) - 1}
    for bucket in range(buckets):
        start = bucket * len(points) // buckets
        end = (bucket + 1) * len(points) // buckets
        selected.update((start, end - 1))
        for dim in range(dimensions):
            def value(i):
                point = points[i]
                return [point["spread"], point["center"], *point["pnl"]][dim]
            valid = [i for i in range(start, end) if value(i) is not None]
            if valid:
                selected.add(min(valid, key=value))
                selected.add(max(valid, key=value))
    return [points[i] for i in sorted(selected)]


def points_for(count, width):
    return [{"ts": i * 2 + (90 if i >= 450 else 0),
             "spread": None if i % 11 == 0 else i % 7 - 3,
             "center": None if i % 13 == 0 else 7,
             "pnl": [None if (i + column) % 9 == 0 else (i * (column + 3)) % 17 - 8
                     for column in range(width)],
             "segment": i // 450}
            for i in range(count)]


class HistoryReductionTests(unittest.TestCase):
    def test_matches_original_across_dimensions_missing_values_and_limits(self):
        for width in (0, 1, 3, 12):
            for count in (0, 1, 17, 899, 900, 901, 2501):
                points = points_for(count, width)
                original = deepcopy(points)
                for limit in (1, 19, 100, 900):
                    with self.subTest(width=width, count=count, limit=limit):
                        sampled = reduce_points(points, limit)
                        self.assertEqual(sampled, original_reduce_points(points, limit))
                        if count <= limit:
                            self.assertIs(sampled, points)
                        else:
                            self.assertIs(sampled[0], points[0])
                            self.assertIs(sampled[-1], points[-1])
                self.assertEqual(points, original, "sampling must not mutate history")

    def test_first_tied_extrema_sparse_series_and_segments_are_retained(self):
        points = [{"ts": i, "spread": None, "center": None,
                   "pnl": [None, None, None], "segment": int(i >= 10)}
                  for i in range(20)]
        for index in (3, 4):
            points[index]["pnl"][0] = -10
        for index in (12, 13):
            points[index]["pnl"][0] = 10
        points[8]["pnl"][1] = 0
        sampled = reduce_points(points, 12)
        self.assertEqual([point["ts"] for point in sampled], [0, 3, 8, 12, 19])
        self.assertEqual([point["segment"] for point in sampled], [0, 0, 0, 1, 1])
        self.assertEqual(sampled, original_reduce_points(points, 12))


if __name__ == "__main__":
    unittest.main()
