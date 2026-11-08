"""Database privilege boundaries and dashboard calculations without a server."""

import json
import unittest
from unittest.mock import patch

from mos_itc import core, databases, postgresql
from mos_itc.core import Error
from mos_itc.i18n import Translator
from mos_itc.terminal import dashboard_lines
from test_mos import Preview


class DatabaseTests(unittest.TestCase):
    def system(self, engine):
        system = Preview()
        data = system.data()
        key = "postgresql_databases" if engine is postgresql else "databases"
        data[key] = {"demo_db": {}, "other_db": {}}
        system.save(data)
        return system

    def test_rejects_unmanaged_and_sql_injection(self):
        for engine in (databases, postgresql):
            system = self.system(engine)
            for value in (
                "mysql",
                "postgres",
                "demo_db`; DROP USER root;",
                "../demo_db",
            ):
                with self.subTest(engine=engine, value=value), self.assertRaises(Error):
                    engine.create_user(system, value, "reader", "read")
            self.assertFalse(system.calls)

    def test_mysql_grant_escapes_database_wildcards(self):
        self.assertEqual(databases.privilege_target("demo_db"), "`demo\\_db`.*")

    def test_created_users_are_scoped_and_secrets_are_not_arguments(self):
        for engine in (databases, postgresql):
            for role in engine.ROLES:
                system = self.system(engine)
                with patch.object(
                    engine.secrets,
                    "token_urlsafe",
                    return_value="private-test-password",
                ):
                    engine.create_user(system, "demo_db", "reader", role)
                account = engine.accounts(system, "demo_db")[0]
                self.assertEqual(account["role"], role)
                self.assertEqual(engine.accounts(system, "other_db"), [])
                self.assertEqual(
                    engine.credentials(system, "demo_db", "reader")["password"],
                    "private-test-password",
                )
                self.assertNotIn(
                    "private-test-password", str([argv for argv, _ in system.calls])
                )
                inputs = "\n".join(str(kw.get("input", "")) for _, kw in system.calls)
                self.assertNotIn("WITH GRANT OPTION", inputs)
                if engine is databases:
                    self.assertNotIn("ON *.*", inputs)
                else:
                    self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE", inputs)

    def test_cross_database_grants_survive_revocation(self):
        for engine in (databases, postgresql):
            system = self.system(engine)
            engine.create_user(system, "demo_db", "reader", "read")
            engine.grant(system, "other_db", "reader", "write")
            engine.revoke(system, "demo_db", "reader")
            self.assertEqual(engine.accounts(system, "demo_db"), [])
            self.assertEqual(engine.accounts(system, "other_db")[0]["role"], "write")

    def test_invalid_role_is_rejected_before_creating_a_user(self):
        for engine in (databases, postgresql):
            system = self.system(engine)
            with self.assertRaises(Error):
                engine.create_user(system, "demo_db", "reader", "SUPERUSER")
            self.assertFalse(system.calls)

    def test_engine_error_does_not_disclose_sql_password(self):
        for engine in (databases, postgresql):
            system = self.system(engine)
            with patch.object(system, "run", side_effect=Error("password SECRET")):
                with self.assertRaises(Error) as result:
                    engine.sql(system, "ALTER USER password SECRET", True)
            self.assertNotIn("SECRET", str(result.exception))

    def test_failed_backup_prevents_deletion(self):
        for engine in (databases, postgresql):
            system = self.system(engine)
            with patch.object(engine, "dump", side_effect=Error("backup failed")):
                with self.assertRaises(Error):
                    engine.remove(system, "demo_db")
            self.assertFalse(system.calls)
            self.assertIn("demo_db", engine.managed(system))

    def test_postgres_permission_change_rolls_back_inventory_on_error(self):
        system = self.system(postgresql)
        postgresql.create_user(system, "demo_db", "reader", "read")
        with patch.object(postgresql, "sql", side_effect=Error("failed")):
            with self.assertRaises(Error):
                postgresql.grant(system, "demo_db", "reader", "admin")
        self.assertEqual(postgresql.accounts(system, "demo_db")[0]["role"], "read")

    def test_mysql_permission_change_rolls_back_inventory_on_error(self):
        system = self.system(databases)
        databases.create_user(system, "demo_db", "reader", "read")
        with patch.object(databases, "sql", side_effect=Error("failed")):
            with self.assertRaises(Error):
                databases.grant(system, "demo_db", "reader", "admin")
        self.assertEqual(databases.accounts(system, "demo_db")[0]["role"], "read")

    def test_primary_site_credentials_are_available_without_logging(self):
        system = self.system(databases)
        data = system.data()
        data["sites"]["demo.test"] = {"user": "mos_demo", "database": "mos_demo"}
        system.save(data)
        path = core.SITES / "demo.test" / ".database.json"
        system.overlay[str(path)] = json.dumps(
            {"username": "mos_demo", "host": "localhost", "password": "secret"}
        )
        self.assertEqual(
            databases.credentials(system, "mos_demo", "mos_demo")["password"], "secret"
        )
        with self.assertRaises(Error):
            databases.revoke(system, "mos_demo", "mos_demo")

    def test_missing_engine_requires_approval_before_apt(self):
        from mos_itc.ui import UI

        system = Preview()
        ui = UI(system)
        with (
            patch("mos_itc.ui.core.installed", return_value=False),
            patch.object(ui, "confirm", return_value=False),
            patch("mos_itc.ui.core.refresh") as refresh,
        ):
            self.assertFalse(ui.ensure_database_engine(postgresql))
        refresh.assert_not_called()

    def test_dashboard_rates_reset_and_zero_swap(self):
        data = {
            "host": "test",
            "time": "2026-11-08T00:00:00+00:00",
            "cpu": 10,
            "ram_used": 50,
            "ram_total": 100,
            "disk_used": 20,
            "disk_total": 100,
            "network": {"eth0": {"received": 2048, "sent": 1024}},
            "load": (1, 2, 3),
            "monotonic": 12,
        }
        previous = dict(
            data, monotonic=10, network={"eth0": {"received": 0, "sent": 4096}}
        )
        result = "\n".join(
            dashboard_lines(data, previous, [0, 100], [], [], Translator("en"))
        )
        self.assertIn("1.0 KiB/s", result)
        self.assertIn("0.0 KiB/s", result)
        self.assertIn("Swap: not configured", result)
