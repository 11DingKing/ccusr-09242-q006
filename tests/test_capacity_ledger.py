"""园区资源容量预约台账测试。

覆盖：容量池与逐月可用量、确认事务校验（拒绝项含来源承诺与剩余量）、
并发确认、跨年度期间、部分释放（含生效月份）、历史月份不可被修订覆盖、
额度转移、项目撤回/阶段回退的自动释放，以及重复操作的幂等性。
"""

import os
import shutil
import tempfile
import threading
import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.database import Base, get_db, configure_sqlite_concurrency
from app.enums import (
    ParkType,
    ProjectStatus,
    Region,
    ReservationEventType,
    ReservationStatus,
    ResourceType,
)
from app.main import app
from app.services import capacity_ledger
from app.services.capacity_ledger import (
    CapacityConflictError,
    ReservationStateError,
)

API = "/api/v1/capacity-ledger"


def add_months(year, month, offset):
    idx = year * 12 + (month - 1) + offset
    return idx // 12, idx % 12 + 1


class CapacityLedgerTestBase(unittest.TestCase):
    """独立临时库 + 依赖覆盖，用例间互不影响。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="ledger_test_")
        cls.engine = create_engine(
            f"sqlite:///{os.path.join(cls._tmpdir, 'test.db')}",
            connect_args={"check_same_thread": False},
        )
        configure_sqlite_concurrency(cls.engine)
        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def _override_get_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_db, None)
        cls.engine.dispose()
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        db = self.Session()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(table.delete())
            db.commit()
        finally:
            db.close()

    # ---------- 数据准备 ----------

    def _fixtures(self, project_status=ProjectStatus.NEGOTIATING, park_name="测试园区"):
        db = self.Session()
        try:
            park = models.IndustrialPark(
                name=park_name, park_type=ParkType.KEY_INDUSTRIAL, city="南宁市"
            )
            entity = models.Entity(
                name=f"{park_name}-企业",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="张三",
                contact_phone="13800000000",
            )
            db.add_all([park, entity])
            db.flush()
            project = models.Project(
                name=f"{park_name}-项目",
                status=project_status,
                investment_direction="果汁加工",
                planned_investment_10k=5000.0,
                park_id=park.id,
                initiator_id=entity.id,
            )
            db.add(project)
            db.commit()
            return park.id, entity.id, project.id
        finally:
            db.close()

    def _make_project(self, park_id, name, status=ProjectStatus.NEGOTIATING):
        db = self.Session()
        try:
            entity = models.Entity(
                name=f"{name}-主体",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="李四",
                contact_phone="13900000000",
            )
            db.add(entity)
            db.flush()
            project = models.Project(
                name=name,
                status=status,
                investment_direction="果干加工",
                planned_investment_10k=3000.0,
                park_id=park_id,
                initiator_id=entity.id,
            )
            db.add(project)
            db.commit()
            return project.id
        finally:
            db.close()

    def _make_pool(self, park_id, resource_type, total):
        db = self.Session()
        try:
            return capacity_ledger.upsert_pool(
                db,
                schemas.CapacityPoolCreate(
                    park_id=park_id,
                    resource_type=resource_type,
                    total_amount=total,
                ),
            ).id
        finally:
            db.close()

    def _make_draft(self, park_id, project_id, resource_type, amount, start, end, **kw):
        db = self.Session()
        try:
            res = capacity_ledger.create_reservation(
                db,
                schemas.CapacityReservationCreate(
                    park_id=park_id,
                    project_id=project_id,
                    resource_type=resource_type,
                    amount=amount,
                    start_year=start[0],
                    start_month=start[1],
                    end_year=end[0],
                    end_month=end[1],
                    **kw,
                ),
            )
            return res.id
        finally:
            db.close()

    # ---------- 读取辅助 ----------

    def _reservation_view(self, rid):
        db = self.Session()
        try:
            r = capacity_ledger.get_reservation(db, rid)
            return {
                "status": r.status,
                "amount": r.amount,
                "released_amount": r.released_amount,
                "entries": [(e.year, e.month, e.amount) for e in r.entries],
                "events": [(e.event_type, e.reason) for e in r.events],
            }
        finally:
            db.close()

    def _availability(self, park_id, resource_type, start, end):
        resp = self.client.get(
            f"{API}/availability",
            params={
                "park_id": park_id,
                "resource_type": resource_type.value,
                "start_year": start[0],
                "start_month": start[1],
                "end_year": end[0],
                "end_month": end[1],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _api_create_reservation(self, park_id, project_id, resource_type, amount, start, end, **extra):
        payload = {
            "park_id": park_id,
            "project_id": project_id,
            "resource_type": resource_type.value,
            "amount": amount,
            "start_year": start[0],
            "start_month": start[1],
            "end_year": end[0],
            "end_month": end[1],
        }
        payload.update(extra)
        return self.client.post(f"{API}/reservations", json=payload)


class PoolAndConfirmTests(CapacityLedgerTestBase):
    def test_pool_draft_confirm_and_availability(self):
        park_id, _, project_id = self._fixtures()
        self._make_pool(park_id, ResourceType.LAND, 1000.0)

        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 5)

        # 暂存不占用容量
        resp = self._api_create_reservation(
            park_id, project_id, ResourceType.LAND, 400.0, start, end
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        draft = resp.json()
        self.assertEqual(draft["status"], ReservationStatus.DRAFT.value)
        self.assertEqual(draft["stage"], ProjectStatus.NEGOTIATING.value)
        avail = self._availability(park_id, ResourceType.LAND, start, end)
        self.assertEqual(len(avail["months"]), 6)
        self.assertTrue(all(m["remaining"] == 1000.0 for m in avail["months"]))

        # 确认后逐月占用，来源可追溯
        resp = self.client.post(f"{API}/reservations/{draft['id']}/confirm", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        confirmed = resp.json()
        self.assertEqual(confirmed["status"], ReservationStatus.CONFIRMED.value)
        self.assertEqual(len(confirmed["entries"]), 6)
        avail = self._availability(park_id, ResourceType.LAND, start, end)
        for month in avail["months"]:
            self.assertEqual(month["occupied"], 400.0)
            self.assertEqual(month["remaining"], 600.0)
            self.assertEqual(len(month["sources"]), 1)
            self.assertEqual(
                month["sources"][0]["reservation_no"], confirmed["reservation_no"]
            )

    def test_confirm_rejection_reports_sources_and_remaining(self):
        park_id, _, project_id = self._fixtures()
        other_id = self._make_project(park_id, "第二项目")
        self._make_pool(park_id, ResourceType.POWER, 1000.0)

        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 2)

        first = self._api_create_reservation(
            park_id, project_id, ResourceType.POWER, 800.0, start, end
        ).json()
        resp = self.client.post(f"{API}/reservations/{first['id']}/confirm", json={})
        self.assertEqual(resp.status_code, 200, resp.text)

        second = self._api_create_reservation(
            park_id, other_id, ResourceType.POWER, 500.0, start, end
        ).json()
        resp = self.client.post(f"{API}/reservations/{second['id']}/confirm", json={})
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["message"], "容量不足，预约无法确认")
        rejections = detail["rejections"]
        self.assertEqual(len(rejections), 3)
        for item in rejections:
            self.assertEqual(item["requested"], 500.0)
            self.assertEqual(item["capacity"], 1000.0)
            self.assertEqual(item["occupied"], 800.0)
            self.assertEqual(item["remaining"], 200.0)
            self.assertEqual(len(item["sources"]), 1)
            self.assertEqual(item["sources"][0]["amount"], 800.0)
            self.assertEqual(item["sources"][0]["project_id"], project_id)

        # 被拒绝的预约仍是暂存，不占用任何容量
        view = self._reservation_view(second["id"])
        self.assertEqual(view["status"], ReservationStatus.DRAFT)
        self.assertEqual(view["entries"], [])

    def test_confirm_without_pool_rejected(self):
        park_id, _, project_id = self._fixtures()
        today = date.today()
        rid = self._make_draft(
            park_id,
            project_id,
            ResourceType.WASTEWATER,
            100.0,
            (today.year, today.month),
            add_months(today.year, today.month, 1),
        )
        resp = self.client.post(f"{API}/reservations/{rid}/confirm", json={})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("尚未登记", resp.json()["detail"])

    def test_draft_never_blocks_capacity(self):
        park_id, _, project_id = self._fixtures()
        other_id = self._make_project(park_id, "第二项目")
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 1)

        # 远超总量的暂存不影响他人确认
        self._make_draft(park_id, project_id, ResourceType.LAND, 99999.0, start, end)
        rid = self._make_draft(park_id, other_id, ResourceType.LAND, 1000.0, start, end)
        resp = self.client.post(f"{API}/reservations/{rid}/confirm", json={})
        self.assertEqual(resp.status_code, 200, resp.text)


class ConcurrencyTests(CapacityLedgerTestBase):
    def test_concurrent_confirm_serializes_and_rejects_overflow(self):
        park_id, _, project_id = self._fixtures()
        other_id = self._make_project(park_id, "第二项目")
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 2)
        months = [start, add_months(*start, 1), add_months(*start, 2)]

        rid_a = self._make_draft(park_id, project_id, ResourceType.LAND, 600.0, start, end)
        rid_b = self._make_draft(park_id, other_id, ResourceType.LAND, 600.0, start, end)

        results = {}
        barrier = threading.Barrier(2)

        def worker(rid, key):
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                capacity_ledger.confirm_reservation(session, rid, idempotency_key=key)
                results[key] = "ok"
            except CapacityConflictError:
                session.rollback()
                results[key] = "conflict"
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                results[key] = f"error:{exc!r}"
            finally:
                session.close()

        threads = [
            threading.Thread(target=worker, args=(rid_a, "key-a")),
            threading.Thread(target=worker, args=(rid_b, "key-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "并发确认未在限定时间内完成")

        self.assertEqual(
            sorted(results.values()),
            ["conflict", "ok"],
            f"两个 600 的确认面对 1000 的总量必须恰好成功一笔: {results}",
        )

        # 最终台账只落账一笔 600，剩余 400
        db = self.Session()
        try:
            avail = capacity_ledger.get_availability(
                db, park_id, ResourceType.LAND, *start, *end
            )
            for month in avail["months"]:
                self.assertEqual(month["occupied"], 600.0)
                self.assertEqual(month["remaining"], 400.0)
                self.assertEqual(len(month["sources"]), 1)
        finally:
            db.close()

        # 失败方保持暂存，可用原幂等键安全重试（仍会因容量不足被拒）
        loser = rid_a if results["key-a"] == "conflict" else rid_b
        session = self.Session()
        try:
            with self.assertRaises(CapacityConflictError):
                capacity_ledger.confirm_reservation(
                    session, loser, idempotency_key="retry"
                )
            session.rollback()
        finally:
            session.close()
        self.assertEqual(self._reservation_view(loser)["status"], ReservationStatus.DRAFT)
        self.assertEqual(len(months), 3)


class CrossYearAndHistoryTests(CapacityLedgerTestBase):
    def test_cross_year_period_entries_and_validation(self):
        park_id, _, project_id = self._fixtures()
        other_id = self._make_project(park_id, "第二项目")
        self._make_pool(park_id, ResourceType.POWER, 100.0)
        today = date(2026, 10, 15)

        rid = self._make_draft(
            park_id, project_id, ResourceType.POWER, 80.0, (2026, 11), (2027, 2)
        )
        db = self.Session()
        try:
            capacity_ledger.confirm_reservation(db, rid, today=today)
        finally:
            db.close()
        view = self._reservation_view(rid)
        self.assertEqual(
            [(y, m) for y, m, _ in view["entries"]],
            [(2026, 11), (2026, 12), (2027, 1), (2027, 2)],
            "跨年度期间应逐月落账",
        )

        # 跨年度可用量查询
        db = self.Session()
        try:
            avail = capacity_ledger.get_availability(
                db, park_id, ResourceType.POWER, 2026, 11, 2027, 2
            )
            self.assertEqual([m["occupied"] for m in avail["months"]], [80.0] * 4)
            self.assertEqual([m["remaining"] for m in avail["months"]], [20.0] * 4)
        finally:
            db.close()

        # 跨年校验：2027-01/02 只剩 20，2027-03 空闲 → 只拒绝前两个月
        rid2 = self._make_draft(
            park_id, other_id, ResourceType.POWER, 50.0, (2027, 1), (2027, 3)
        )
        db = self.Session()
        try:
            with self.assertRaises(CapacityConflictError) as ctx:
                capacity_ledger.confirm_reservation(db, rid2, today=today)
            db.rollback()
        finally:
            db.close()
        rejected_at = [(r["year"], r["month"]) for r in ctx.exception.rejections]
        self.assertEqual(rejected_at, [(2027, 1), (2027, 2)])
        self.assertEqual(ctx.exception.rejections[0]["remaining"], 20.0)

    def test_historical_months_not_overwritten_by_revision(self):
        park_id, _, project_id = self._fixtures()
        self._make_pool(park_id, ResourceType.LAND, 1000.0)

        # 2026-09 确认 2026-07 起的期间：7、8 月已是历史，不落账
        rid = self._make_draft(
            park_id, project_id, ResourceType.LAND, 500.0, (2026, 7), (2026, 12)
        )
        db = self.Session()
        try:
            capacity_ledger.confirm_reservation(db, rid, today=date(2026, 9, 10))
        finally:
            db.close()
        view = self._reservation_view(rid)
        self.assertEqual(
            [(y, m) for y, m, _ in view["entries"]],
            [(2026, 9), (2026, 10), (2026, 11), (2026, 12)],
            "确认时已过月份不产生台账明细",
        )

        # 时间推进到 2026-11：修订额度为 800，历史月份（9、10 月）保持 500 不变
        db = self.Session()
        try:
            capacity_ledger.revise_reservation(
                db,
                rid,
                schemas.CapacityReservationUpdate(amount=800.0),
                today=date(2026, 11, 5),
            )
        finally:
            db.close()
        entries = {(y, m): a for y, m, a in self._reservation_view(rid)["entries"]}
        self.assertEqual(entries[(2026, 9)], 500.0, "历史月份明细不可被修订覆盖")
        self.assertEqual(entries[(2026, 10)], 500.0, "历史月份明细不可被修订覆盖")
        self.assertEqual(entries[(2026, 11)], 800.0)
        self.assertEqual(entries[(2026, 12)], 800.0)

        # 历史月份的占用来源仍然是原承诺
        db = self.Session()
        try:
            avail = capacity_ledger.get_availability(
                db, park_id, ResourceType.LAND, 2026, 9, 2026, 12
            )
            by_month = {(m["year"], m["month"]): m for m in avail["months"]}
            self.assertEqual(by_month[(2026, 10)]["occupied"], 500.0)
            self.assertEqual(by_month[(2026, 12)]["occupied"], 800.0)
        finally:
            db.close()

    def test_confirm_fully_past_period_rejected(self):
        park_id, _, project_id = self._fixtures()
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        rid = self._make_draft(
            park_id, project_id, ResourceType.LAND, 100.0, (2026, 1), (2026, 6)
        )
        db = self.Session()
        try:
            with self.assertRaises(ReservationStateError):
                capacity_ledger.confirm_reservation(db, rid, today=date(2026, 9, 1))
            db.rollback()
        finally:
            db.close()


class ReleaseTests(CapacityLedgerTestBase):
    def test_partial_release_and_effective_month(self):
        park_id, _, project_id = self._fixtures()
        self._make_pool(park_id, ResourceType.WASTEWATER, 1000.0)
        today = date(2026, 9, 10)

        rid = self._make_draft(
            park_id, project_id, ResourceType.WASTEWATER, 500.0, (2026, 9), (2026, 12)
        )
        db = self.Session()
        try:
            capacity_ledger.confirm_reservation(db, rid, today=today)
            # 部分释放 200：未来月份降为 300，预约仍有效
            capacity_ledger.release_reservation(
                db, rid, amount=200.0, reason="企业分期建设", today=today
            )
        finally:
            db.close()
        view = self._reservation_view(rid)
        self.assertEqual(view["status"], ReservationStatus.CONFIRMED)
        self.assertEqual(view["amount"], 300.0)
        self.assertEqual(view["released_amount"], 200.0)
        self.assertEqual([a for _, _, a in view["entries"]], [300.0] * 4)
        self.assertIn(
            ReservationEventType.PARTIALLY_RELEASED, [e for e, _ in view["events"]]
        )

        # 指定生效月份的部分释放：生效月之前保持原额度
        db = self.Session()
        try:
            capacity_ledger.release_reservation(
                db,
                rid,
                amount=100.0,
                effective_from=(2026, 11),
                reason="二期减产",
                today=today,
            )
        finally:
            db.close()
        entries = {(y, m): a for y, m, a in self._reservation_view(rid)["entries"]}
        self.assertEqual(entries[(2026, 9)], 300.0)
        self.assertEqual(entries[(2026, 10)], 300.0)
        self.assertEqual(entries[(2026, 11)], 200.0)
        self.assertEqual(entries[(2026, 12)], 200.0)

        # 释放生效月份不能早于当月（历史月份不可释放）
        db = self.Session()
        try:
            with self.assertRaises(ReservationStateError):
                capacity_ledger.release_reservation(
                    db, rid, amount=50.0, effective_from=(2026, 8), today=today
                )
            db.rollback()
        finally:
            db.close()

    def test_full_release_keeps_history_and_frees_future(self):
        park_id, _, project_id = self._fixtures()
        self._make_pool(park_id, ResourceType.LAND, 1000.0)

        rid = self._make_draft(
            park_id, project_id, ResourceType.LAND, 500.0, (2026, 9), (2026, 12)
        )
        db = self.Session()
        try:
            capacity_ledger.confirm_reservation(db, rid, today=date(2026, 9, 10))
            # 时间推进到 11 月后全部释放：9、10 月明细作为历史保留
            capacity_ledger.release_reservation(
                db, rid, reason="项目终止", today=date(2026, 11, 3)
            )
        finally:
            db.close()
        view = self._reservation_view(rid)
        self.assertEqual(view["status"], ReservationStatus.RELEASED)
        self.assertEqual(view["released_amount"], 500.0)
        self.assertEqual(
            view["entries"],
            [(2026, 9, 500.0), (2026, 10, 500.0)],
            "历史月份明细必须保留，未来月份明细删除",
        )

        db = self.Session()
        try:
            avail = capacity_ledger.get_availability(
                db, park_id, ResourceType.LAND, 2026, 9, 2026, 12
            )
            by_month = {(m["year"], m["month"]): m for m in avail["months"]}
            self.assertEqual(by_month[(2026, 9)]["occupied"], 500.0, "历史占用如实保留")
            self.assertEqual(by_month[(2026, 11)]["occupied"], 0.0, "未来月份已释放")
            self.assertEqual(by_month[(2026, 12)]["remaining"], 1000.0)
        finally:
            db.close()


class IdempotencyTests(CapacityLedgerTestBase):
    def test_duplicate_operations_are_idempotent(self):
        park_id, _, project_id = self._fixtures()
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 2)

        # 重复登记：同一幂等键只生成一张预约单
        payload = dict(
            park_id=park_id,
            project_id=project_id,
            resource_type=ResourceType.LAND.value,
            amount=500.0,
            start_year=start[0],
            start_month=start[1],
            end_year=end[0],
            end_month=end[1],
            idempotency_key="create-1",
        )
        r1 = self.client.post(f"{API}/reservations", json=payload)
        r2 = self.client.post(f"{API}/reservations", json=payload)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        listing = self.client.get(
            f"{API}/reservations", params={"park_id": park_id}
        ).json()
        self.assertEqual(len(listing), 1)
        rid = r1.json()["id"]

        # 重复确认：占用量不翻倍
        for _ in range(2):
            resp = self.client.post(
                f"{API}/reservations/{rid}/confirm",
                json={"idempotency_key": "confirm-1"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
        avail = self._availability(park_id, ResourceType.LAND, start, end)
        self.assertEqual(avail["months"][0]["occupied"], 500.0)
        view = self._reservation_view(rid)
        confirms = [e for e, _ in view["events"] if e == ReservationEventType.CONFIRMED]
        self.assertEqual(len(confirms), 1, "重复确认不得重复入账")

        # 同一幂等键的部分释放只生效一次
        for _ in range(2):
            resp = self.client.post(
                f"{API}/reservations/{rid}/release",
                json={"amount": 100.0, "idempotency_key": "release-1"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
        view = self._reservation_view(rid)
        self.assertEqual(view["released_amount"], 100.0)
        self.assertEqual(view["amount"], 400.0)

        # 重复全部释放：第二次幂等返回，不产生额外事件
        for _ in range(2):
            resp = self.client.post(
                f"{API}/reservations/{rid}/release",
                json={"idempotency_key": "release-final"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(
                resp.json()["status"], ReservationStatus.RELEASED.value
            )
        view = self._reservation_view(rid)
        releases = [e for e, _ in view["events"] if e == ReservationEventType.RELEASED]
        self.assertEqual(len(releases), 1)
        self.assertEqual(view["released_amount"], 500.0)


class TransferTests(CapacityLedgerTestBase):
    def test_full_and_partial_transfer(self):
        park_id, _, project_id = self._fixtures()
        other_id = self._make_project(park_id, "受让项目")
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 3)

        # 部分转移 200：源预约降为 300，受让方获得 200
        rid = self._make_draft(park_id, project_id, ResourceType.LAND, 500.0, start, end)
        resp = self.client.post(f"{API}/reservations/{rid}/confirm", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.post(
            f"{API}/reservations/{rid}/transfer",
            json={"target_project_id": other_id, "amount": 200.0, "reason": "分期转让"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["source"]["status"], ReservationStatus.CONFIRMED.value)
        self.assertEqual(result["source"]["amount"], 300.0)
        self.assertEqual(result["target"]["status"], ReservationStatus.CONFIRMED.value)
        self.assertEqual(result["target"]["amount"], 200.0)
        self.assertEqual(result["target"]["transfer_from_id"], rid)
        self.assertEqual(result["target"]["project_id"], other_id)

        # 转移前后园区总占用不变（400 → 300+200）
        avail = self._availability(park_id, ResourceType.LAND, start, end)
        self.assertEqual(avail["months"][0]["occupied"], 500.0)
        self.assertEqual(len(avail["months"][0]["sources"]), 2)

        # 全部转移：源预约转为已转移，额度整体过户
        resp = self.client.post(
            f"{API}/reservations/{rid}/transfer",
            json={"target_project_id": other_id},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["source"]["status"], ReservationStatus.TRANSFERRED.value)
        view = self._reservation_view(rid)
        self.assertEqual(view["entries"], [], "全部转移后源预约不再持有未来月份")
        avail = self._availability(park_id, ResourceType.LAND, start, end)
        self.assertEqual(avail["months"][0]["occupied"], 500.0, "转移不改变总占用")

    def test_transfer_validation_errors(self):
        park_id, _, project_id = self._fixtures()
        other_park_id, _, other_project_id = self._fixtures(park_name="另一园区")
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 1)

        rid = self._make_draft(park_id, project_id, ResourceType.LAND, 500.0, start, end)

        # 暂存状态不可转移
        resp = self.client.post(
            f"{API}/reservations/{rid}/transfer",
            json={"target_project_id": other_project_id},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        self.client.post(f"{API}/reservations/{rid}/confirm", json={})

        # 跨园区转移被拒
        resp = self.client.post(
            f"{API}/reservations/{rid}/transfer",
            json={"target_project_id": other_project_id},
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("不可跨园区转移", resp.json()["detail"])

        # 转移给自身被拒
        resp = self.client.post(
            f"{API}/reservations/{rid}/transfer",
            json={"target_project_id": project_id},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        # 超额转移被拒
        target_id = self._make_project(park_id, "受让项目")
        resp = self.client.post(
            f"{API}/reservations/{rid}/transfer",
            json={"target_project_id": target_id, "amount": 9999.0},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_transfer_idempotent_replay(self):
        park_id, _, project_id = self._fixtures()
        target_id = self._make_project(park_id, "受让项目")
        self._make_pool(park_id, ResourceType.POWER, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 1)

        rid = self._make_draft(park_id, project_id, ResourceType.POWER, 400.0, start, end)
        self.client.post(f"{API}/reservations/{rid}/confirm", json={})
        payload = {"target_project_id": target_id, "idempotency_key": "transfer-1"}
        r1 = self.client.post(f"{API}/reservations/{rid}/transfer", json=payload)
        r2 = self.client.post(f"{API}/reservations/{rid}/transfer", json=payload)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r1.json()["target"]["id"], r2.json()["target"]["id"])
        listing = self.client.get(
            f"{API}/reservations", params={"project_id": target_id}
        ).json()
        self.assertEqual(len(listing), 1, "重复转移不得生成多笔受让预约")


class StageChangeReleaseTests(CapacityLedgerTestBase):
    def _change_status(self, project_id, to_status, reason="阶段调整"):
        resp = self.client.post(
            f"/api/v1/projects/{project_id}/status",
            json={"to_status": to_status.value, "reason": reason, "operator": "测试员"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_stage_regression_releases_later_stage_reservations(self):
        park_id, _, project_id = self._fixtures(project_status=ProjectStatus.ATTRACTING_INVESTMENT)
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 2)

        self._change_status(project_id, ProjectStatus.NEGOTIATING)
        self._change_status(project_id, ProjectStatus.ESTABLISHED)

        # 已立项阶段确认 400；洽谈中阶段暂存一笔
        confirmed_id = self._make_draft(
            park_id, project_id, ResourceType.LAND, 400.0, start, end,
            stage=ProjectStatus.ESTABLISHED,
        )
        resp = self.client.post(f"{API}/reservations/{confirmed_id}/confirm", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        draft_id = self._make_draft(
            park_id, project_id, ResourceType.LAND, 100.0, start, end,
            stage=ProjectStatus.NEGOTIATING,
        )

        # 回退到洽谈中：已立项阶段的预约按规则自动释放，洽谈中阶段的暂存保留
        self._change_status(project_id, ProjectStatus.NEGOTIATING, reason="企业资金未到位")
        view = self._reservation_view(confirmed_id)
        self.assertEqual(view["status"], ReservationStatus.RELEASED)
        self.assertEqual(view["entries"], [])
        auto_events = [
            reason
            for event, reason in view["events"]
            if event == ReservationEventType.AUTO_RELEASED
        ]
        self.assertEqual(len(auto_events), 1)
        self.assertIn("回退", auto_events[0])
        self.assertEqual(
            self._reservation_view(draft_id)["status"], ReservationStatus.DRAFT
        )
        avail = self._availability(park_id, ResourceType.LAND, start, end)
        self.assertEqual(avail["months"][0]["occupied"], 0.0)

        # 撤回到招商中：剩余暂存也一并释放
        self._change_status(project_id, ProjectStatus.ATTRACTING_INVESTMENT, reason="项目撤回")
        view = self._reservation_view(draft_id)
        self.assertEqual(view["status"], ReservationStatus.RELEASED)
        auto_events = [
            reason
            for event, reason in view["events"]
            if event == ReservationEventType.AUTO_RELEASED
        ]
        self.assertIn("撤回", auto_events[0])

    def test_forward_stage_change_keeps_reservations(self):
        park_id, _, project_id = self._fixtures(project_status=ProjectStatus.ATTRACTING_INVESTMENT)
        self._make_pool(park_id, ResourceType.LAND, 1000.0)
        today = date.today()
        start = (today.year, today.month)
        end = add_months(today.year, today.month, 1)

        self._change_status(project_id, ProjectStatus.NEGOTIATING)
        rid = self._make_draft(park_id, project_id, ResourceType.LAND, 400.0, start, end)
        self.client.post(f"{API}/reservations/{rid}/confirm", json={})

        self._change_status(project_id, ProjectStatus.ESTABLISHED, reason="完成立项")
        view = self._reservation_view(rid)
        self.assertEqual(view["status"], ReservationStatus.CONFIRMED)
        self.assertEqual(len(view["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
