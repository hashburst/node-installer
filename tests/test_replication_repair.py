import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from replication.hb_replication_db import ReplicationDB
from replication.hb_replication_policy import PlacementPolicy
import replication.hb_replication_controller as ctrlmod

GiB = 1024 ** 3
NOW = 2_000_000_000


def registry_node(node_id, role, capacity_class):
    return {
        "node_id": node_id,
        "name": node_id,
        "role": role,
        "capacity_class": capacity_class,
        "enabled": True,
        "replication_token": "test-token",
    }


class ControllerHarness:
    def __init__(self, testcase, nodes, *, n=3, m=2, margin=0):
        self.testcase = testcase
        self.tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry = root / "storage-nodes.json"
        self.registry.write_text(json.dumps({"nodes": nodes}))
        self.db = ReplicationDB(str(root / "controller.sqlite3"))
        testcase.addCleanup(self.db._conn.close)
        self.ctl = ctrlmod.Controller(
            self.db,
            str(self.registry),
            PlacementPolicy(n, m, prefer_edge_for_extra=True, safety_margin_bytes=margin),
            mode="pin-only",
        )
        self.ctl.sync_registry()

    def heartbeat(self, node_id, total_gib=100, used_gib=0, at=NOW):
        self.db.heartbeat(node_id, total_gib * GiB, used_gib * GiB, now=at)

    def register(self, cid="bafy-object", size_gib=1, source="p1"):
        self.db.register_object(cid, size_gib * GiB, 3, 2, source_node=source)
        return cid

    def jobs(self, node_id):
        return self.db.pending_jobs(node_id, now=NOW + 10_000)

    def confirm_all_pin_jobs(self):
        for n in self.db.nodes():
            for job in self.jobs(n["node_id"]):
                if job["operation"] == "PIN":
                    self.db.apply_job_report(n["node_id"], {
                        "job_id": job["job_id"],
                        "state": "pinned",
                        "lease_until": job["lease_until"],
                    })


class ReplicationRepairTests(unittest.TestCase):
    def setUp(self):
        self.time_patch = mock.patch.object(ctrlmod.time, "time", return_value=NOW)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.cfg_patchers = [
            mock.patch.object(ctrlmod, "NODE_STALE", 90),
            mock.patch.object(ctrlmod, "EDGE_GRACE", 6 * 3600),
            mock.patch.object(ctrlmod, "COMMITTABLE_GRACE", 60),
            mock.patch.object(ctrlmod, "VERIFY_INTERVAL", 900),
        ]
        for p in self.cfg_patchers:
            p.start(); self.addCleanup(p.stop)

    def base_nodes(self):
        return [
            registry_node("p1", "primary", "committable"),
            registry_node("p2", "secondary", "committable"),
            registry_node("p3", "secondary", "committable"),
            registry_node("e1", "edge", "best-effort"),
            registry_node("e2", "edge", "best-effort"),
        ]

    def healthy_n3_m2(self):
        h = ControllerHarness(self, self.base_nodes())
        for nid in ("p1", "p2", "p3", "e1", "e2"):
            h.heartbeat(nid)
        cid = h.register()
        h.ctl.reconcile_cid(cid)
        h.confirm_all_pin_jobs()
        h.ctl.reconcile_cid(cid)
        status = h.ctl.object_status(cid)
        self.assertEqual("healthy", status["state"])
        self.assertEqual(3, status["confirmed_total"])
        self.assertGreaterEqual(status["confirmed_committable"], 2)
        return h, cid

    def test_n3_m2_reaches_healthy_after_confirmations(self):
        self.healthy_n3_m2()

    def test_committable_failure_beyond_grace_repairs_to_another_committable(self):
        h, cid = self.healthy_n3_m2()
        reps = h.db.replica_rows(cid)
        comm = [r for r in reps if r["class_at_assignment"] == "committable" and r["node_id"] != "p1"]
        self.assertTrue(comm)
        failed = comm[0]["node_id"]
        # Make the failed committable stale beyond its short grace.
        h.heartbeat(failed, at=NOW - 120)
        h.ctl.reconcile_cid(cid)
        rows = {r["node_id"]: r for r in h.db.replica_rows(cid)}
        self.assertEqual("missing", rows[failed]["state"])
        replacements = [
            r for r in rows.values()
            if r["class_at_assignment"] == "committable" and r["node_id"] != failed and r["state"] == "assigned"
        ]
        self.assertTrue(replacements, rows)

    def test_edge_offline_within_six_hour_grace_does_not_trigger_replacement(self):
        h, cid = self.healthy_n3_m2()
        rows = h.db.replica_rows(cid)
        edge = next(r for r in rows if r["class_at_assignment"] == "best-effort")
        h.heartbeat(edge["node_id"], at=NOW - 3600)
        before = len(h.db.replica_rows(cid))
        h.ctl.reconcile_cid(cid)
        rows = {r["node_id"]: r for r in h.db.replica_rows(cid)}
        self.assertEqual("grace", rows[edge["node_id"]]["state"])
        self.assertEqual(before, len(rows), "grace must not allocate replacement yet")
        status = h.ctl.object_status(cid)
        self.assertEqual("degraded_total", status["state"])
        self.assertEqual(2, status["confirmed_total"])

    def test_edge_offline_beyond_grace_triggers_replacement(self):
        h, cid = self.healthy_n3_m2()
        rows = h.db.replica_rows(cid)
        edge = next(r for r in rows if r["class_at_assignment"] == "best-effort")
        h.heartbeat(edge["node_id"], at=NOW - (6 * 3600 + 1))
        h.ctl.reconcile_cid(cid)
        rows = {r["node_id"]: r for r in h.db.replica_rows(cid)}
        self.assertEqual("missing", rows[edge["node_id"]]["state"])
        replacements = [r for r in rows.values() if r["node_id"] != edge["node_id"] and r["state"] == "assigned"]
        self.assertTrue(replacements, rows)

    def test_insufficient_capacity_keeps_object_degraded_and_creates_no_fake_replica(self):
        nodes = [
            registry_node("p1", "primary", "committable"),
            registry_node("p2", "secondary", "committable"),
            registry_node("e1", "edge", "best-effort"),
        ]
        h = ControllerHarness(self, nodes, margin=0)
        h.heartbeat("p1", total_gib=10, used_gib=0)
        h.heartbeat("p2", total_gib=1, used_gib=1)
        h.heartbeat("e1", total_gib=1, used_gib=1)
        cid = h.register(size_gib=2)
        h.ctl.reconcile_cid(cid)
        status = h.ctl.object_status(cid)
        self.assertEqual("degraded_committable", status["state"])
        self.assertEqual(1, status["confirmed_total"])
        self.assertEqual([], h.jobs("p2"))
        self.assertEqual([], h.jobs("e1"))

    def test_failed_pin_is_not_counted_and_repair_selects_another_node(self):
        h = ControllerHarness(self, self.base_nodes())
        for nid in ("p1", "p2", "p3", "e1", "e2"):
            h.heartbeat(nid)
        cid = h.register()
        h.ctl.reconcile_cid(cid)
        # Fail one non-source PIN job and confirm the rest.
        jobs = []
        for nid in ("p2", "p3", "e1", "e2"):
            jobs.extend((nid, j) for j in h.jobs(nid) if j["operation"] == "PIN")
        self.assertGreaterEqual(len(jobs), 2)
        failed_nid, failed_job = jobs[0]
        h.db.apply_job_report(failed_nid, {
            "job_id": failed_job["job_id"],
            "state": "failed",
            "lease_until": failed_job["lease_until"],
            "error": "mock pin failure",
        })
        for nid, job in jobs[1:]:
            h.db.apply_job_report(nid, {
                "job_id": job["job_id"],
                "state": "pinned",
                "lease_until": job["lease_until"],
            })
        h.ctl.reconcile_cid(cid)
        status = h.ctl.object_status(cid)
        failed_rep = next(r for r in status["replicas"] if r["node_id"] == failed_nid)
        self.assertEqual("failed", failed_rep["state"])
        self.assertLess(status["confirmed_total"], len([r for r in status["replicas"] if r["desired"]]))
        assigned_elsewhere = [r for r in status["replicas"] if r["node_id"] != failed_nid and r["state"] == "assigned"]
        self.assertTrue(assigned_elsewhere, status)

    def test_periodic_verify_job_is_created_for_stale_confirmation(self):
        h, cid = self.healthy_n3_m2()
        # Force one confirmed replica verification timestamp old.
        target = h.db.replica_rows(cid)[0]
        h.db._conn.execute(
            "UPDATE replicas SET last_verified_at=? WHERE cid=? AND node_id=?",
            (NOW - 901, cid, target["node_id"]),
        )
        h.ctl.reconcile_cid(cid)
        verify = [j for j in h.jobs(target["node_id"]) if j["operation"] == "VERIFY"]
        self.assertEqual(1, len(verify))

    def test_under_threshold_is_reported_not_hidden_by_desired_assignments(self):
        h = ControllerHarness(self, self.base_nodes())
        for nid in ("p1", "p2", "p3", "e1", "e2"):
            h.heartbeat(nid)
        cid = h.register()
        h.ctl.reconcile_cid(cid)
        status = h.ctl.object_status(cid)
        self.assertEqual("degraded_committable", status["state"])
        self.assertEqual(1, status["confirmed_total"])
        self.assertEqual(1, status["confirmed_committable"])
        self.assertGreaterEqual(len(status["replicas"]), 3, "desired replicas may exist but must not be counted confirmed")

    def test_create_pin_assignment_is_idempotent_under_race(self):
        nodes = [registry_node("p1", "primary", "committable"), registry_node("p2", "secondary", "committable")]
        h = ControllerHarness(self, nodes)
        h.heartbeat("p1"); h.heartbeat("p2")
        cid = h.register()
        results = []
        errors = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait()
                results.append(h.db.create_pin_assignment(cid, "p2", "committable"))
            except Exception as exc:  # pragma: no cover - failure path is asserted
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual([], errors)
        self.assertEqual(1, sum(1 for r in results if r is not None))
        rows = h.db._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE cid=? AND node_id=? AND operation='PIN' AND state IN ('pending','retry')",
            (cid, "p2"),
        ).fetchone()[0]
        self.assertEqual(1, rows)

    def test_duplicate_success_job_report_is_idempotent(self):
        nodes = [registry_node("p1", "primary", "committable"), registry_node("p2", "secondary", "committable")]
        h = ControllerHarness(self, nodes)
        h.heartbeat("p1"); h.heartbeat("p2")
        cid = h.register()
        job_id = h.db.create_pin_assignment(cid, "p2", "committable")
        self.assertIsNotNone(job_id)
        report = {"job_id": job_id, "state": "pinned"}
        h.db.apply_job_report("p2", report)
        h.db.apply_job_report("p2", report)
        rep = next(r for r in h.db.replica_rows(cid) if r["node_id"] == "p2")
        self.assertEqual("pinned", rep["state"])
        job = h.db._conn.execute("SELECT state,attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual("done", job[0])
        self.assertEqual(0, job[1])

    def test_duplicate_failed_job_report_does_not_double_count_attempt(self):
        nodes = [registry_node("p1", "primary", "committable"), registry_node("p2", "secondary", "committable")]
        h = ControllerHarness(self, nodes)
        h.heartbeat("p1"); h.heartbeat("p2")
        cid = h.register()
        job_id = h.db.create_pin_assignment(cid, "p2", "committable")
        self.assertIsNotNone(job_id)
        report = {"job_id": job_id, "state": "failed", "error": "same delivery"}
        h.db.apply_job_report("p2", report)
        first = h.db._conn.execute("SELECT attempts,next_attempt_at FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        h.db.apply_job_report("p2", report)
        second = h.db._conn.execute("SELECT attempts,next_attempt_at FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(first[0], second[0], "duplicate failure report must not consume another retry attempt")
        self.assertEqual(first[1], second[1], "duplicate failure report must preserve the same backoff deadline")

    def test_leased_job_is_not_redelivered_before_lease_expiry(self):
        nodes = [registry_node("p1", "primary", "committable"), registry_node("p2", "secondary", "committable")]
        h = ControllerHarness(self, nodes)
        h.heartbeat("p1"); h.heartbeat("p2")
        cid = h.register()
        job_id = h.db.create_pin_assignment(cid, "p2", "committable")
        first = h.db.pending_jobs("p2", now=NOW)
        self.assertEqual([job_id], [j["job_id"] for j in first])
        second = h.db.pending_jobs("p2", now=NOW + 60)
        self.assertEqual([], second, "active lease must suppress duplicate delivery")
        third = h.db.pending_jobs("p2", now=NOW + 121)
        self.assertEqual([job_id], [j["job_id"] for j in third])
        self.assertNotEqual(first[0]["lease_until"], third[0]["lease_until"])

    def test_stale_failed_report_from_old_lease_is_ignored_after_redelivery(self):
        nodes = [registry_node("p1", "primary", "committable"), registry_node("p2", "secondary", "committable")]
        h = ControllerHarness(self, nodes)
        h.heartbeat("p1"); h.heartbeat("p2")
        cid = h.register()
        job_id = h.db.create_pin_assignment(cid, "p2", "committable")

        first_job = h.db.pending_jobs("p2", now=NOW)[0]
        first_report = {
            "job_id": job_id,
            "state": "failed",
            "lease_until": first_job["lease_until"],
            "error": "first attempt",
        }
        h.db.apply_job_report("p2", first_report)
        row = h.db._conn.execute(
            "SELECT attempts FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(1, row[0])

        # Make the legitimate retry due, then lease it as a new delivery.
        h.db._conn.execute(
            "UPDATE jobs SET next_attempt_at=0, lease_until=0 WHERE job_id=?", (job_id,)
        )
        retry_job = h.db.pending_jobs("p2", now=NOW + 500)[0]
        self.assertNotEqual(first_job["lease_until"], retry_job["lease_until"])

        # A delayed replay from attempt 1 must not mutate attempt 2.
        h.db.apply_job_report("p2", first_report)
        row = h.db._conn.execute(
            "SELECT attempts,lease_until FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(1, row[0])
        self.assertEqual(retry_job["lease_until"], row[1])

        # The current attempt may fail legitimately and consume exactly one retry.
        h.db.apply_job_report("p2", {
            "job_id": job_id,
            "state": "failed",
            "lease_until": retry_job["lease_until"],
            "error": "second attempt",
        })
        row = h.db._conn.execute(
            "SELECT attempts FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertEqual(2, row[0])


class ReferenceIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = ReplicationDB(str(Path(self.tmp.name) / "controller.sqlite3"))
        self.addCleanup(self.db._conn.close)
        self.db.upsert_node(registry_node("p1", "primary", "committable"))

    def test_duplicate_reference_registration_does_not_inflate_refcount(self):
        self.db.register_object("bafy-ref", GiB, 3, 2, source_node="p1", reference_id="file-1")
        self.db.register_object("bafy-ref", GiB, 3, 2, source_node="p1", reference_id="file-1")
        self.assertEqual(1, self.db.object("bafy-ref")["refcount"])

    def test_two_logical_references_increment_and_release_idempotently(self):
        self.db.register_object("bafy-ref", GiB, 3, 2, source_node="p1", reference_id="file-1")
        self.db.register_object("bafy-ref", GiB, 3, 2, source_node="p1", reference_id="file-2")
        self.assertEqual(2, self.db.object("bafy-ref")["refcount"])
        first = self.db.release_object("bafy-ref", "file-1")
        replay = self.db.release_object("bafy-ref", "file-1")
        self.assertEqual(1, first["refcount"])
        self.assertEqual(1, replay["refcount"])
        self.assertFalse(replay["released"])
        last = self.db.release_object("bafy-ref", "file-2")
        self.assertEqual(0, last["refcount"])

    def test_reference_id_cannot_be_reused_for_different_cid(self):
        self.db.register_object("bafy-a", GiB, 3, 2, source_node="p1", reference_id="file-1")
        with self.assertRaises(ValueError):
            self.db.register_object("bafy-b", GiB, 3, 2, source_node="p1", reference_id="file-1")


if __name__ == "__main__":
    unittest.main()
