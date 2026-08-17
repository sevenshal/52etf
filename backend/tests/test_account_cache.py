"""web_accounts 有效账户集内存缓存测试。

is_valid_account 带 24h TTL 缓存；账户增删改（create/update/delete）成功后
invalidate_account_cache() 立即失效，保证页面上的删除/停用即时生效。
"""

from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.app.api import account as account_api
from src.app.api.account import AccountCreate, AccountUpdate
from src.core.database import Base, WebAccount


class AccountCacheTest(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.session_patch = patch.object(account_api, "SessionLocal", self.session_factory)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.engine.dispose)
        # 清掉模块级缓存，避免用例之间相互污染
        account_api.invalidate_account_cache()

        db = self.session_factory()
        try:
            db.add(WebAccount(account_id="enabled-account", enabled=True))
            db.add(WebAccount(account_id="disabled-account", enabled=False))
            db.commit()
        finally:
            db.close()

    def test_first_call_loads_from_db(self):
        self.assertTrue(account_api.is_valid_account("enabled-account"))
        self.assertFalse(account_api.is_valid_account("disabled-account"))
        self.assertFalse(account_api.is_valid_account("unknown-account"))

    def test_cache_stays_warm_until_invalidated(self):
        self.assertTrue(account_api.is_valid_account("enabled-account"))  # 加载缓存

        # 绕过 API 直接改 DB：缓存未失效前仍返回旧结果
        db = self.session_factory()
        try:
            db.delete(db.get(WebAccount, "enabled-account"))
            db.commit()
        finally:
            db.close()

        self.assertTrue(account_api.is_valid_account("enabled-account"))

        # 手动失效后重新加载
        account_api.invalidate_account_cache()
        self.assertFalse(account_api.is_valid_account("enabled-account"))

    def test_ttl_reloads_after_24h(self):
        clock = [1000.0]
        with patch.object(account_api.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(account_api.is_valid_account("enabled-account"))  # t=1000 加载

            db = self.session_factory()
            try:
                db.add(WebAccount(account_id="brand-new", enabled=True))
                db.commit()
            finally:
                db.close()

            # 未到 24h：新账户不可见
            self.assertFalse(account_api.is_valid_account("brand-new"))

            # 超过 24h：自动重新加载
            clock[0] += 24 * 60 * 60 + 1
            self.assertTrue(account_api.is_valid_account("brand-new"))

    def test_account_writes_invalidate_cache(self):
        self.assertTrue(account_api.is_valid_account("enabled-account"))  # 预热缓存

        # 创建后立即生效
        account_api.create_account(
            AccountCreate(account_id="fresh", note="", enabled=True), _="admin"
        )
        self.assertTrue(account_api.is_valid_account("fresh"))

        # 停用后立即失效
        account_api.update_account("fresh", AccountUpdate(enabled=False), _="admin")
        self.assertFalse(account_api.is_valid_account("fresh"))

        # 删除后立即失效
        account_api.delete_account("fresh", _="admin")
        self.assertFalse(account_api.is_valid_account("fresh"))

    def test_reload_failure_keeps_old_cache(self):
        self.assertTrue(account_api.is_valid_account("enabled-account"))  # 预热缓存

        with patch.object(account_api, "_load_enabled_account_ids", side_effect=RuntimeError("db down")):
            # 加载失败：保留旧缓存，返回旧结果而不是异常
            self.assertTrue(account_api.is_valid_account("enabled-account"))
