from auth_helpers import authorize_owner
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from server import main


class RecentActivitiesTest(unittest.TestCase):
    def setUp(self):
        stack = patch.object(main.app.app, 'middleware_stack', None)
        stack.start()
        self.addCleanup(stack.stop)

    def test_clear_history_preserves_downloads_and_survives_memory_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'history.db'
            processed = root / 'processed'
            processed.mkdir()
            file = processed / 'saved.png'
            file.write_bytes(b'original-image')
            with patch.multiple(main, DB_PATH=str(db), PROCESSED_DIR=str(processed),
                                STATS={'recent_activities': []}):
                main.init_db()
                client = TestClient(main.app)
                authorize_owner(self, client, root)
                response = client.post('/api/activity/clear-all')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['deleted'], 0)
                main.STATS['recent_activities'] = []
                self.assertEqual(main.get_stats()['recent_activities'], [])
                self.assertEqual(client.get('/api/download/saved.png').content, b'original-image')
                self.assertEqual(file.read_bytes(), b'original-image')
                (processed / 'new.png').touch()
                self.assertEqual([a['filename'] for a in main.get_stats()['recent_activities']], ['new.png'])
                self.assertEqual(main.get_stats()['protected_files'], 2)

    def test_deleted_files_do_not_reappear_and_metrics_use_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'history.db'
            processed = root / 'processed'
            processed.mkdir()
            (processed / 'delete.png').touch()
            with patch.multiple(main, DB_PATH=str(db), PROCESSED_DIR=str(processed),
                                STATS={'recent_activities': [{'filename': 'delete.png'}, {'filename': 'missing.png'}]}):
                main.init_db()
                client = TestClient(main.app)
                authorize_owner(self, client, root)
                self.assertEqual(main.get_stats()['protected_files'], 1)
                self.assertEqual(client.post('/api/file/delete', data={'filename': 'delete.png'}).json()['status'], 'success')
                self.assertEqual(main.get_stats()['recent_activities'], [])
                self.assertEqual(main.get_stats()['protected_files'], 0)

    def test_hidden_items_stay_off_history_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.db"
            processed = root / "processed"
            processed.mkdir()
            visible = processed / "visible.png"
            hidden = processed / "hidden.png"
            visible.touch()
            hidden.touch()
            os.utime(hidden, (2000000000, 2000000000))
            with patch.multiple(main, DB_PATH=str(db), PROCESSED_DIR=str(processed),
                                STATS={"recent_activities": [{"filename": hidden.name}]}):
                main.init_db()
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("INSERT INTO hidden_activities VALUES (?)", (hidden.name,))
                self.assertEqual([item["filename"] for item in main.get_stats()["recent_activities"]],
                                 [visible.name])
                self.assertEqual(main.list_processed_activities(1, excluded={hidden.name})[0]["filename"],
                                 visible.name)
                self.assertTrue(hidden.is_file())


if __name__ == "__main__":
    unittest.main()
