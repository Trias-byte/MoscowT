"""Check the distinctions that could produce false same-direction connections."""
import unittest

from build_same_direction_stops import Pattern, analyze


def pattern(route, path, stop_nodes, direction=0, exit_only=False):
    stops = [dict(node_id=node, name=f"Stop {node}", order=i + 1,
                  position=path.index(node), role="stop") for i, node in enumerate(stop_nodes)]
    if exit_only:
        stops[0]["role"] = "stop_exit_only"
    return Pattern(route, route * 100, direction, path, stops)


class SameDirectionTests(unittest.TestCase):
    def run_pair(self, a, b):
        nodes = {node: dict(lat=55.7 + node * .0001, lon=37.6) for node in set(a.path + b.path)}
        return analyze([a, b], nodes, "2026-02-09")

    def test_opposite_local_direction_numbers_can_share_forward_track(self):
        rows, _, _ = self.run_pair(pattern(11, [1, 2, 3], [1, 3], 0), pattern(12, [1, 2, 3], [1, 3], 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["direction_a"], rows[0]["direction_b"]), (0, 1))
        self.assertEqual(rows[0]["next_common_stop_id"], "osm-node-3")

    def test_same_direction_numbers_do_not_prove_same_travel_direction(self):
        rows, _, _ = self.run_pair(pattern(1, [1, 2, 3], [1, 2, 3]), pattern(2, [3, 2, 1], [3, 2, 1]))
        self.assertEqual(rows, [])

    def test_divergence_and_rejoining_are_not_a_continuous_common_path(self):
        rows, excluded, _ = self.run_pair(pattern(1, [1, 2, 3, 5], [1, 5]), pattern(2, [1, 2, 4, 5], [1, 5]))
        self.assertEqual(rows, [])
        self.assertIn("no_common_alighting_stop_before_divergence", {e["reason"] for e in excluded})

    def test_skipped_intermediate_stop_keeps_track_verified_connection(self):
        rows, _, _ = self.run_pair(pattern(1, [1, 2, 3], [1, 2, 3]), pattern(2, [1, 2, 3], [1, 3]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["same_immediate_next_stop"], 0)

    def test_alighting_only_origin_is_not_a_departure_connection(self):
        rows, excluded, _ = self.run_pair(pattern(1, [1, 2], [1, 2], exit_only=True), pattern(2, [1, 2], [1, 2]))
        self.assertEqual(rows, [])
        self.assertIn("alighting_only_at_origin", {e["reason"] for e in excluded})

    def test_coincident_names_without_same_node_do_not_join(self):
        a, b = pattern(1, [1, 2], [1, 2]), pattern(2, [3, 4], [3, 4])
        for sa, sb in zip(a.stops, b.stops):
            sa["name"] = sb["name"] = "Same name"
        rows, _, _ = self.run_pair(a, b)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
