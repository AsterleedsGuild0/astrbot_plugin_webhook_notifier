"""Registry module tests - no AstrBot dependency."""

from __future__ import annotations

import os
import json
import tempfile
import uuid

import pytest

from core.models import EndpointStatus
from core.registry import (
    REGISTRY_FILENAME,
    EndpointRegistry,
    build_endpoint_path,
    owner_path_hash,
)


@pytest.fixture
def registry():
    """创建一个使用临时目录的 registry 实例。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = EndpointRegistry(tmpdir)
        yield reg


class TestRegistryCreate:
    def test_create_private_endpoint(self, registry):
        """创建私聊 endpoint 应返回记录和 token。"""
        record, token = registry.create_private_endpoint(
            name="test_private",
            path="test_private",
            owner_user_id="user_001",
            target_umo="aiocqhttp:FriendMessage:10001",
        )
        assert record.name == "test_private"
        assert record.status == EndpointStatus.ACTIVE.value
        assert record.token_hash != ""
        assert record.owner_user_id == "user_001"
        assert len(record.targets) == 1
        assert record.targets[0].umo == "aiocqhttp:FriendMessage:10001"
        assert token.startswith("whn_")

    def test_create_private_endpoint_no_target(self, registry):
        """创建私聊 endpoint 时必须有 target UMO。"""
        record, token = registry.create_private_endpoint(
            name="test_private2",
            path="test_private2",
            owner_user_id="user_001",
            target_umo="aiocqhttp:FriendMessage:20002",
        )
        assert len(record.targets) == 1
        assert record.targets[0].name == "default"

    def test_create_pending_verification(self, registry):
        """创建群聊待验证申请应返回记录、request_id 和 code。"""
        record, request_id, code = registry.create_pending_verification(
            name="test_group",
            path="test_group",
            owner_user_id="user_002",
            target_group_id="123456789",
        )
        assert record.name == "test_group"
        assert record.status == EndpointStatus.PENDING_VERIFICATION.value
        assert record.token_hash == ""  # 验证通过前无 token hash
        assert record.pending_request_id == request_id
        assert record.pending_code == code
        assert uuid.UUID(request_id).version == 4
        assert len(code) == 6

    def test_same_name_different_owner_isolated(self, registry):
        """不同用户可以创建同名 endpoint，记录按 owner+name 隔离。"""
        record1, _ = registry.create_private_endpoint(
            name="dup_test",
            path=build_endpoint_path("user_001", "dup_test"),
            owner_user_id="user_001",
            target_umo="aiocqhttp:FriendMessage:10001",
        )
        record2, _ = registry.create_private_endpoint(
            name="dup_test",
            path=build_endpoint_path("user_002", "dup_test"),
            owner_user_id="user_002",
            target_umo="aiocqhttp:FriendMessage:20002",
        )
        assert record1.path != record2.path
        assert (
            registry.get_by_owner_name("user_001", "dup_test").owner_user_id
            == "user_001"
        )
        assert (
            registry.get_by_owner_name("user_002", "dup_test").owner_user_id
            == "user_002"
        )

    def test_build_endpoint_path_uses_owner_hash(self):
        """URL path 应按 owner hash 隔离且不直接暴露用户 ID。"""
        path = build_endpoint_path("123456789", "omp-test")
        assert path == f"u/{owner_path_hash('123456789')}/omp-test"
        assert "123456789" not in path


class TestRegistryVerify:
    def test_verify_success(self, registry):
        """群聊验证成功应返回 token。"""
        record, request_id, code = registry.create_pending_verification(
            name="verify_test",
            path="verify_test",
            owner_user_id="user_003",
            target_group_id="111111",
        )
        status, msg, token = registry.verify_group_endpoint(
            request_id=request_id,
            code=code,
            verify_user_id="user_003",
            verify_group_id="111111",
            group_target_umo="aiocqhttp:GroupMessage:111111",
        )
        assert status == "ok"
        assert token is not None
        assert token.startswith("whn_")
        # 验证成功后 endpoint 应为 active
        record = registry.get_by_name("verify_test")
        assert record.status == EndpointStatus.ACTIVE.value
        assert record.token_hash != ""

    def test_verify_wrong_code(self, registry):
        """错误验证码应失败。"""
        record, request_id, code = registry.create_pending_verification(
            name="verify_wrong_code",
            path="verify_wrong_code",
            owner_user_id="user_004",
            target_group_id="222222",
        )
        wrong_code = "000000"
        status, msg, token = registry.verify_group_endpoint(
            request_id=request_id,
            code=wrong_code,
            verify_user_id="user_004",
            verify_group_id="222222",
            group_target_umo="aiocqhttp:GroupMessage:222222",
        )
        assert status == "error"
        assert "不匹配" in msg
        assert token is None

    def test_verify_wrong_user(self, registry):
        """非申请者验证应失败。"""
        record, request_id, code = registry.create_pending_verification(
            name="verify_wrong_user",
            path="verify_wrong_user",
            owner_user_id="user_005",
            target_group_id="333333",
        )
        status, msg, token = registry.verify_group_endpoint(
            request_id=request_id,
            code=code,
            verify_user_id="attacker_user",
            verify_group_id="333333",
            group_target_umo="aiocqhttp:GroupMessage:333333",
        )
        assert status == "error"
        assert "执行者不是申请者" in msg

    def test_verify_wrong_group(self, registry):
        """错误目标群应失败。"""
        record, request_id, code = registry.create_pending_verification(
            name="verify_wrong_group",
            path="verify_wrong_group",
            owner_user_id="user_006",
            target_group_id="444444",
        )
        status, msg, token = registry.verify_group_endpoint(
            request_id=request_id,
            code=code,
            verify_user_id="user_006",
            verify_group_id="999999",
            group_target_umo="aiocqhttp:GroupMessage:999999",
        )
        assert status == "error"
        assert "当前群不是申请目标群" in msg

    def test_verify_nonexistent_request(self, registry):
        """不存在的 request_id 应失败。"""
        status, msg, token = registry.verify_group_endpoint(
            request_id="nonexistent-request-id",
            code="abcdef",
            verify_user_id="user_007",
            verify_group_id="555555",
            group_target_umo="aiocqhttp:GroupMessage:555555",
        )
        assert status == "error"
        assert "不存在" in msg

    def test_verify_expired(self, registry):
        """过期的待验证申请应失败。"""
        # 使用一个已经过期的 pending request
        # 为了测试，我们手动构造一个时间过期的 pending 记录
        record, request_id, code = registry.create_pending_verification(
            name="verify_expired",
            path="verify_expired",
            owner_user_id="user_008",
            target_group_id="666666",
        )
        # 模拟 pending 记录过期
        import datetime

        past_time = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=30)
        ).isoformat()
        registry._pending[request_id]["expires_at"] = past_time

        status, msg, token = registry.verify_group_endpoint(
            request_id=request_id,
            code=code,
            verify_user_id="user_008",
            verify_group_id="666666",
            group_target_umo="aiocqhttp:GroupMessage:666666",
        )
        assert status == "error"
        assert "过期" in msg


class TestRegistryQuery:
    def test_get_by_name(self, registry):
        registry.create_private_endpoint(
            name="query_test",
            path="query_test",
            owner_user_id="user_010",
            target_umo="aiocqhttp:FriendMessage:10010",
        )
        record = registry.get_by_name("query_test")
        assert record is not None
        assert record.name == "query_test"

    def test_get_by_name_not_found(self, registry):
        assert registry.get_by_name("nonexistent") is None

    def test_get_by_owner_name(self, registry):
        registry.create_private_endpoint(
            name="query_same",
            path=build_endpoint_path("user_a", "query_same"),
            owner_user_id="user_a",
            target_umo="aiocqhttp:FriendMessage:10010",
        )
        registry.create_private_endpoint(
            name="query_same",
            path=build_endpoint_path("user_b", "query_same"),
            owner_user_id="user_b",
            target_umo="aiocqhttp:FriendMessage:10011",
        )
        assert (
            registry.get_by_owner_name("user_a", "query_same").owner_user_id == "user_a"
        )
        assert (
            registry.get_by_owner_name("user_b", "query_same").owner_user_id == "user_b"
        )

    def test_get_by_path(self, registry):
        registry.create_private_endpoint(
            name="path_test",
            path="my-custom-path",
            owner_user_id="user_011",
            target_umo="aiocqhttp:FriendMessage:10011",
        )
        record = registry.get_by_path("my-custom-path")
        assert record is not None
        assert record.name == "path_test"

    def test_list_by_owner(self, registry):
        registry.create_private_endpoint(
            name="owner_test_1",
            path="owner_test_1",
            owner_user_id="user_020",
            target_umo="aiocqhttp:FriendMessage:10020",
        )
        registry.create_private_endpoint(
            name="owner_test_2",
            path="owner_test_2",
            owner_user_id="user_020",
            target_umo="aiocqhttp:FriendMessage:10021",
        )
        registry.create_private_endpoint(
            name="other_owner",
            path="other_owner",
            owner_user_id="user_999",
            target_umo="aiocqhttp:FriendMessage:10999",
        )
        records = registry.list_by_owner("user_020")
        assert len(records) == 2
        names = {r.name for r in records}
        assert names == {"owner_test_1", "owner_test_2"}

    def test_list_visible_by_owner_hides_terminal_records(self, registry):
        registry.create_private_endpoint(
            name="visible_active",
            path="visible_active",
            owner_user_id="user_visible",
            target_umo="aiocqhttp:FriendMessage:10020",
        )
        registry.create_private_endpoint(
            name="hidden_revoked",
            path="hidden_revoked",
            owner_user_id="user_visible",
            target_umo="aiocqhttp:FriendMessage:10021",
        )
        registry.revoke_endpoint("hidden_revoked", "user_visible")

        visible = registry.list_visible_by_owner("user_visible")
        assert {r.name for r in visible} == {"visible_active"}
        assert {r.name for r in registry.list_by_owner("user_visible")} == {
            "visible_active",
            "hidden_revoked",
        }

    def test_count_active(self, registry):
        registry.create_private_endpoint(
            name="active_1",
            path="active_1",
            owner_user_id="user_030",
            target_umo="aiocqhttp:FriendMessage:10030",
        )
        assert registry.count_active() == 1
        registry.create_private_endpoint(
            name="active_2",
            path="active_2",
            owner_user_id="user_030",
            target_umo="aiocqhttp:FriendMessage:10031",
        )
        assert registry.count_active() == 2

    def test_list_all_for_admin_crosses_owners_and_returns_copies(self, registry):
        first, _ = registry.create_private_endpoint(
            name="admin_a",
            path="u/a/admin_a",
            owner_user_id="owner_a",
            target_umo="aiocqhttp:FriendMessage:10001",
        )
        registry.create_private_endpoint(
            name="admin_b",
            path="u/b/admin_b",
            owner_user_id="owner_b",
            target_umo="aiocqhttp:GroupMessage:20002",
        )

        records = registry.list_all_for_admin()

        assert {record.owner_user_id for record in records} == {"owner_a", "owner_b"}
        records[0].status = EndpointStatus.REVOKED.value
        assert first.status == EndpointStatus.ACTIVE.value

    def test_is_endpoint_active(self, registry):
        registry.create_private_endpoint(
            name="active_check",
            path="active_check",
            owner_user_id="user_040",
            target_umo="aiocqhttp:FriendMessage:10040",
        )
        assert registry.is_endpoint_active("active_check") is True


class TestRegistryRevoke:
    def test_revoke_active(self, registry):
        registry.create_private_endpoint(
            name="revoke_active",
            path="revoke_active",
            owner_user_id="user_050",
            target_umo="aiocqhttp:FriendMessage:10050",
        )
        success, msg = registry.revoke_endpoint("revoke_active", "user_050")
        assert success is True
        record = registry.get_by_name("revoke_active")
        assert record.status == EndpointStatus.REVOKED.value
        assert record.revoked_at is not None
        assert registry.is_endpoint_active("revoke_active") is False

    def test_revoke_pending(self, registry):
        record, request_id, code = registry.create_pending_verification(
            name="revoke_pending",
            path="revoke_pending",
            owner_user_id="user_060",
            target_group_id="777777",
        )
        success, msg = registry.revoke_endpoint("revoke_pending", "user_060")
        assert success is True
        record = registry.get_by_name("revoke_pending")
        assert record.status == EndpointStatus.REVOKED.value

    def test_revoke_other_owner(self, registry):
        registry.create_private_endpoint(
            name="other_owner_revoke",
            path="other_owner_revoke",
            owner_user_id="user_070",
            target_umo="aiocqhttp:FriendMessage:10070",
        )
        success, msg = registry.revoke_endpoint("other_owner_revoke", "user_999")
        assert success is False
        assert "不存在" in msg

    def test_revoke_nonexistent(self, registry):
        success, msg = registry.revoke_endpoint("nonexistent_endpoint", "user_080")
        assert success is False
        assert "不存在" in msg

    def test_admin_revoke_by_exact_path(self, registry):
        record, _ = registry.create_private_endpoint(
            name="admin_revoke",
            path="u/admin/exact-path",
            owner_user_id="owner_admin_target",
            target_umo="aiocqhttp:FriendMessage:10050",
        )

        success, msg = registry.revoke_endpoint_by_path("u/admin/exact-path")

        assert success is True
        assert "已撤销" in msg
        assert record.status == EndpointStatus.REVOKED.value
        assert registry.is_endpoint_active(record.name, record.owner_user_id) is False

    def test_admin_revoke_accepts_single_leading_slash(self, registry):
        record, _ = registry.create_private_endpoint(
            name="leading_slash",
            path="u/admin/leading-slash",
            owner_user_id="owner_leading",
            target_umo="aiocqhttp:FriendMessage:10051",
        )

        success, _ = registry.revoke_endpoint_by_path("/u/admin/leading-slash")

        assert success is True
        assert record.status == EndpointStatus.REVOKED.value

    def test_admin_revoke_nonexistent_path(self, registry):
        success, msg = registry.revoke_endpoint_by_path("u/admin/missing")
        assert success is False
        assert "不存在" in msg

    def test_admin_revoke_is_idempotent(self, registry):
        registry.create_private_endpoint(
            name="idempotent",
            path="u/admin/idempotent",
            owner_user_id="owner_idempotent",
            target_umo="aiocqhttp:FriendMessage:10052",
        )
        assert registry.revoke_endpoint_by_path("u/admin/idempotent")[0] is True

        success, msg = registry.revoke_endpoint_by_path("u/admin/idempotent")

        assert success is True
        assert "无需重复" in msg

    def test_admin_revoke_rejects_duplicate_path_without_changes(self, registry):
        first, _ = registry.create_private_endpoint(
            name="duplicate_a",
            path="u/corrupt/duplicate",
            owner_user_id="owner_duplicate_a",
            target_umo="aiocqhttp:FriendMessage:10053",
        )
        second, _ = registry.create_private_endpoint(
            name="duplicate_b",
            path="u/corrupt/duplicate",
            owner_user_id="owner_duplicate_b",
            target_umo="aiocqhttp:FriendMessage:10054",
        )

        success, msg = registry.revoke_endpoint_by_path("u/corrupt/duplicate")

        assert success is False
        assert "重复记录" in msg
        assert first.status == EndpointStatus.ACTIVE.value
        assert second.status == EndpointStatus.ACTIVE.value

    def test_admin_revoke_by_owner_name_is_exact_and_idempotent(self, registry):
        selected, _ = registry.create_private_endpoint(
            name="same_name",
            path="u/selected/same_name",
            owner_user_id="selected_owner",
            target_umo="aiocqhttp:FriendMessage:10055",
        )
        other, _ = registry.create_private_endpoint(
            name="same_name",
            path="u/other/same_name",
            owner_user_id="other_owner",
            target_umo="aiocqhttp:FriendMessage:10056",
        )

        success, msg = registry.revoke_endpoint_by_owner_name(
            "selected_owner", "same_name"
        )
        repeated_success, repeated_msg = registry.revoke_endpoint_by_owner_name(
            "selected_owner", "same_name"
        )

        assert success is True
        assert "已撤销" in msg
        assert repeated_success is True
        assert "无需重复" in repeated_msg
        assert selected.status == EndpointStatus.REVOKED.value
        assert other.status == EndpointStatus.ACTIVE.value

    def test_admin_revoke_by_owner_name_not_found(self, registry):
        registry.create_private_endpoint(
            name="known_name",
            path="u/known/known_name",
            owner_user_id="known_owner",
            target_umo="aiocqhttp:FriendMessage:10057",
        )

        missing_owner = registry.revoke_endpoint_by_owner_name(
            "missing_owner", "known_name"
        )
        missing_name = registry.revoke_endpoint_by_owner_name(
            "known_owner", "missing_name"
        )

        assert missing_owner[0] is False
        assert "不存在" in missing_owner[1]
        assert missing_name[0] is False
        assert "不存在" in missing_name[1]

    def test_revoked_name_and_path_can_be_reused(self, registry):
        record1, token1 = registry.create_private_endpoint(
            name="reuse_after_revoke",
            path=build_endpoint_path("user_reuse", "reuse_after_revoke"),
            owner_user_id="user_reuse",
            target_umo="aiocqhttp:FriendMessage:10180",
        )
        assert (
            registry.is_owner_name_available("user_reuse", "reuse_after_revoke")
            is False
        )
        assert registry.is_path_available(record1.path) is False

        success, msg = registry.revoke_endpoint("reuse_after_revoke", "user_reuse")
        assert success is True
        assert (
            registry.is_owner_name_available("user_reuse", "reuse_after_revoke") is True
        )
        assert registry.is_path_available(record1.path) is True

        record2, token2 = registry.create_private_endpoint(
            name="reuse_after_revoke",
            path=record1.path,
            owner_user_id="user_reuse",
            target_umo="aiocqhttp:FriendMessage:10180",
        )
        assert record2.status == EndpointStatus.ACTIVE.value
        assert record2.revoked_at is None
        assert token2 != token1


class TestRegistryRotate:
    def test_rotate_active(self, registry):
        registry.create_private_endpoint(
            name="rotate_test",
            path="rotate_test",
            owner_user_id="user_090",
            target_umo="aiocqhttp:FriendMessage:10090",
        )
        old_record = registry.get_by_name("rotate_test")
        old_hash = old_record.token_hash

        success, new_token = registry.rotate_token("rotate_test", "user_090")
        assert success is True
        assert new_token.startswith("whn_")

        new_record = registry.get_by_name("rotate_test")
        assert new_record.token_hash != old_hash

    def test_rotate_wrong_owner(self, registry):
        registry.create_private_endpoint(
            name="rotate_owner",
            path="rotate_owner",
            owner_user_id="user_100",
            target_umo="aiocqhttp:FriendMessage:10100",
        )
        success, msg = registry.rotate_token("rotate_owner", "user_999")
        assert success is False

    def test_rotate_pending(self, registry):
        record, request_id, code = registry.create_pending_verification(
            name="rotate_pending",
            path="rotate_pending",
            owner_user_id="user_110",
            target_group_id="888888",
        )
        success, msg = registry.rotate_token("rotate_pending", "user_110")
        assert success is False
        assert "只有 active 状态" in msg

    def test_rotate_revoked(self, registry):
        registry.create_private_endpoint(
            name="rotate_revoked",
            path="rotate_revoked",
            owner_user_id="user_120",
            target_umo="aiocqhttp:FriendMessage:10120",
        )
        registry.revoke_endpoint("rotate_revoked", "user_120")
        success, msg = registry.rotate_token("rotate_revoked", "user_120")
        assert success is False


class TestRegistryPersistence:
    def test_save_and_reload(self):
        """registry 应能保存到磁盘并重新加载。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg1 = EndpointRegistry(tmpdir)
            reg1.create_private_endpoint(
                name="persist_test",
                path="persist_test",
                owner_user_id="user_200",
                target_umo="aiocqhttp:FriendMessage:10200",
            )

            # 创建第二个实例，重新加载
            reg2 = EndpointRegistry(tmpdir)
            record = reg2.get_by_name("persist_test")
            assert record is not None
            assert record.status == EndpointStatus.ACTIVE.value
            assert record.owner_user_id == "user_200"
            assert len(record.targets) == 1

    def test_persistence_with_revoke(self):
        """撤销的 endpoint 在重载后仍为 revoked 状态。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg1 = EndpointRegistry(tmpdir)
            reg1.create_private_endpoint(
                name="persist_revoke",
                path="persist_revoke",
                owner_user_id="user_210",
                target_umo="aiocqhttp:FriendMessage:10210",
            )
            reg1.revoke_endpoint("persist_revoke", "user_210")

            reg2 = EndpointRegistry(tmpdir)
            record = reg2.get_by_name("persist_revoke")
            assert record is not None
            assert record.status == EndpointStatus.REVOKED.value
            assert record.revoked_at is not None

    def test_server_secret_persistence(self):
        """server_secret 应在同一数据目录保持一致。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg1 = EndpointRegistry(tmpdir)
            secret1 = reg1.server_secret
            assert len(secret1) > 0

            reg2 = EndpointRegistry(tmpdir)
            secret2 = reg2.server_secret
            assert secret1 == secret2

    def test_legacy_render_fields_are_ignored_and_dropped_on_save(self):
        """旧 registry 中的 render_mode/template 字段应被忽略，重新保存后清理。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = os.path.join(tmpdir, REGISTRY_FILENAME)
            legacy_data = {
                "records": {
                    "user_legacy\u001flegacy_endpoint": {
                        "name": "legacy_endpoint",
                        "path": "u/legacy/legacy_endpoint",
                        "provider": "omp",
                        "token_hash": "legacy_hash",
                        "token_hash_algorithm": "HMAC-SHA256",
                        "owner_user_id": "user_legacy",
                        "targets": [
                            {
                                "name": "default",
                                "umo": "aiocqhttp:FriendMessage:10001",
                            }
                        ],
                        "render_mode": "text",
                        "template": "legacy.html",
                        "status": EndpointStatus.ACTIVE.value,
                        "created_at": "2026-07-10T00:00:00+00:00",
                    }
                },
                "pending": {},
            }
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            reg = EndpointRegistry(tmpdir)
            record = reg.get_by_name("legacy_endpoint")
            assert record is not None
            assert not hasattr(record, "render_mode")
            assert not hasattr(record, "template")

            reg.rotate_token("legacy_endpoint", "user_legacy")
            saved = json.loads(open(registry_path, encoding="utf-8").read())
            saved_record = saved["records"]["user_legacy\u001flegacy_endpoint"]
            assert "render_mode" not in saved_record
            assert "template" not in saved_record


class TestStalePending:
    def test_expire_stale_pending(self, registry):
        registry.create_pending_verification(
            name="stale_test",
            path="stale_test",
            owner_user_id="user_300",
            target_group_id="999999",
        )
        # 手动让 pending 过期
        import datetime

        past_time = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=30)
        ).isoformat()
        for rid in list(registry._pending.keys()):
            registry._pending[rid]["expires_at"] = past_time

        cleaned = registry.expire_stale_pending()
        assert cleaned >= 1
        record = registry.get_by_name("stale_test")
        assert record.status == EndpointStatus.EXPIRED.value
