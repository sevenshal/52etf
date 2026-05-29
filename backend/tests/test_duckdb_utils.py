import os
import tempfile
from unittest import TestCase

import duckdb
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from src.core.duckdb_utils import (
    DUCKDB_CONFIG_MISMATCH_MESSAGE,
    connect_duckdb,
    connect_duckdb_engine,
)


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

    def test_sqlalchemy_engine_connection_uses_same_duckdb_configuration(self):
        engine = create_engine(
            "duckdb:///:memory:",
            creator=lambda: connect_duckdb_engine(self.path, prefer_read_only=False),
            poolclass=NullPool,
        )

        read_connection = connect_duckdb(self.path, prefer_read_only=True)
        try:
            with engine.connect() as sql_connection:
                row = sql_connection.execute(text("SELECT COUNT(*) FROM rows")).fetchone()
        finally:
            read_connection.close()

        self.assertEqual(0, row[0])
