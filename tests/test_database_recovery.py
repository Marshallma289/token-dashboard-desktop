import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import DashboardDB


class DatabaseRecoveryTests(unittest.TestCase):
    def test_malformed_database_is_backed_up_before_rebuild(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "usage.sqlite3"

            connection = sqlite3.connect(str(db_path))
            connection.execute("CREATE TABLE payload(value BLOB)")
            connection.executemany("INSERT INTO payload VALUES(randomblob(4000))", [()] * 100)
            connection.commit()
            connection.close()

            data = bytearray(db_path.read_bytes())
            data[4096 + 100 : 4096 + 200] = b"X" * 100
            db_path.write_bytes(data)

            db = DashboardDB(db_path, roots=[root / "sessions"])
            try:
                recovery = db.database_recovery
                self.assertIsNotNone(recovery)
                self.assertEqual(recovery["status"], "rebuilt")
                self.assertEqual(len(recovery["backups"]), 1)
                backup = Path(recovery["backups"][0])
                self.assertTrue(backup.name.startswith("usage.sqlite3.corrupt-"))
                self.assertTrue(backup.name.endswith(".bak"))
                self.assertTrue(backup.exists())
                self.assertEqual(db.counts()["usage_records"], 0)
            finally:
                db.close()

            check = sqlite3.connect(str(db_path))
            try:
                self.assertEqual(check.execute("PRAGMA quick_check(1)").fetchone()[0], "ok")
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
