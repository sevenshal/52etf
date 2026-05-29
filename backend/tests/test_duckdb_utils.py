import os
import tempfile
from unittest import TestCase

import duckdb

from src.core.duckdb_utils import DUCKDB_CONFIG_MISMATCH_MESSAGE, connect_duckdb


class DuckDBUtilsTest(TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(self.path)
        connection = duckdb.connect(self.path, read_only=False)
        try:
            connection.execute("CREATE TABLE rows(id INTEGER)")
        finally:
            connection.close()

    def tearDown(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def test_prefer_read_only_connection_does_not_block_writer(self):
        read_connection = connect_duckdb(self.path, prefer_read_only=True)
        try:
            write_connection = connect_duckdb(self.path, prefer_read_only=False)
            try:
                write_connection.execute("INSERT INTO rows VALUES (1)")
            finally:
                write_connection.close()
        finally:
            read_connection.close()

    def test_write_connection_does_not_fallback_to_read_only(self):
        read_only_connection = duckdb.connect(self.path, read_only=True)
        try:
            with self.assertRaises(Exception) as context:
                connect_duckdb(self.path, prefer_read_only=False)
        finally:
            read_only_connection.close()

        self.assertIn(DUCKDB_CONFIG_MISMATCH_MESSAGE, str(context.exception))
