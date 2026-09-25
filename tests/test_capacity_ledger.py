"""园区容量预约台账的接口与并发测试。

覆盖：确认拒绝溯源、并发确认串行化、跨年度期间、部分释放、
重复操作、批量确认原子性、历史月份封存、阶段变更/撤回自动释放、
项目间转移、暂存修订与确认后不可改、逐月可用量查询。
"""

import os
import tempfile
import threading
import unittest
import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
from app import models
from app.enums import (
    MilestoneStatus,
    MilestoneType,
    ParkType,
    ProjectStatus,
    Region,
    ReservationStatus,
    ResourceType,
)

API = "/api/v1/capacity-ledger"


def month_seq(start: date, count: int):
    """从 start 所在月的下一月起，返回 count 个 (year, month)。"""
    idx = start.year * 12 + start.month  # start 的下一月
    return [((idx + i) // 12, (idx + i) % 12 + 1) for i in range(count)]


class CapacityLedgerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "ledger_test.db")
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionFactory = sessionmaker(
            bind=cls.engine, autocommit=False, autoflush=False
        )

        def override_get_db():
            db = cls.SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_db, None)
        cls.engine.dispose()
        cls.tmp.cleanup()

    def setUp(self):
        with self.SessionFactory() as db:
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(table.delete())
            db.commit()
        self.client = TestClient(app)

    # ------------------------------------------------------------------
    # 构造工具
    # ------------------------------------------------------------------

    def _make_park(self):
        with self.SessionFactory() as db:
            park = models.IndustrialPark(
                name=f"园区-{uuid.uuid4().hex[:8]}",
                park_type=ParkType.KEY_INDUSTRIAL,
                city="南宁市",
            )
            db.add(park)
            db.commit()
            return park.id

    def _make_project(self, park_id, status=ProjectStatus.NEGOTIATING):
        with self.SessionFactory() as db:
            entity = models.Entity(
                name=f"主体-{uuid.uuid4().hex[:8]}",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="张三",
                contact_phone="0771-0000000",
            )
            db.add(entity)
            db.flush()
            project = models.Project(
                name=f"项目-{uuid.uuid4().hex[:8]}",
                investment_direction="果汁加工",
                planned_investment_10k=1000.0,
                park_id=park_id,
                initiator_id=entity.id,
                status=status,
            )
            db.add(project)
            db.commit()
            return project.id

    def _register_capacity(self, park_id, total, start, end=None, resource="用地", as_of=None):
        payload = {
            "park_id": park_id,
            "resource_type": resource,
            "total_amount": total,
            "effective_from_year": start[0],
            "effective_from_month": start[1],
        }
        if end:
            payload["effective_to_year"] = end[0]
            payload["effective_to_month"] = end[1]
        if as_of:
            payload["as_of"] = as_of
        return self.client.post(f"{API}/capacities", json=payload)

    def _create_reservation(self, park_id, project_id, start, end, amount,
                            resource="用地", stage="洽谈中", as_of=None):
        payload = {
            "park_id": park_id,
            "project_id": project_id,
            "resource_type": resource,
            "stage": stage,
            "start_year": start[0],
            "start_month": start[1],
            "end_year": end[0],
            "end_month": end[1],
            "monthly_amount": amount,
            "created_by": "招商一组",
        }
        if as_of:
            payload["as_of"] = as_of
        return self.client.post(f"{API}/reservations", json=payload)

    def _confirm(self, ids, as_of=None):
        payload = {"reservation_ids": ids, "operator": "招商运营"}
        if as_of:
            payload["as_of"] = as_of
        return self.client.post(f"{API}/reservations/confirm", json=payload)

    def _availability(self, park_id, start, end, resource="用地"):
        resp = self.client.get(
            f"{API}/parks/{park_id}/availability",
            params={
                "resource_type": resource,
                "start_year": start[0],
                "start_month": start[1],
                "end_year": end[0],
                "end_month": end[1],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return { (m["year"], m["month"]): m for m in resp.json()["months"] }

    def _future_months(self, count):
        return month_seq(date.today(), count)

    # ------------------------------------------------------------------
    # 拒绝项溯源
    # ------------------------------------------------------------------

    def test_confirm_rejection_returns_sources_and_remaining(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self.assertEqual(self._register_capacity(park_id, 100, start).status_code, 200)

        pa = self._make_project(park_id)
        pb = self._make_project(park_id)
        ra = self._create_reservation(park_id, pa, start, end, 80).json()
        rb = self._create_reservation(park_id, pb, start, end, 50).json()

        resp = self._confirm([ra["id"]])
        self.assertEqual(resp.json()["status"], "confirmed")

        resp = self._confirm([rb["id"]])
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["confirmed"], [])
        self.assertEqual(len(body["rejections"]), 2)
        item = body["rejections"][0]
        self.assertEqual(item["reservation_id"], rb["id"])
        self.assertEqual(item["total_capacity"], 100)
        self.assertEqual(item["committed_amount"], 80)
        self.assertEqual(item["remaining_amount"], 20)
        self.assertEqual(len(item["sources"]), 1)
        source = item["sources"][0]
        self.assertEqual(source["reservation_id"], ra["id"])
        self.assertEqual(source["reservation_code"], ra["reservation_code"])
        self.assertEqual(source["amount"], 80)
        self.assertEqual(source["project_id"], pa)

        # 被拒绝的预约保持暂存，且未写入确认事件
        detail = self.client.get(f"{API}/reservations/{rb['id']}").json()
        self.assertEqual(detail["status"], "暂存")
        self.assertEqual(
            [e["event_type"] for e in detail["events"]], ["暂存登记"]
        )

    # ------------------------------------------------------------------
    # 并发确认
    # ------------------------------------------------------------------

    def test_concurrent_confirm_is_serialized(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self.assertEqual(self._register_capacity(park_id, 100, start).status_code, 200)

        p1 = self._make_project(park_id)
        p2 = self._make_project(park_id)
        r1 = self._create_reservation(park_id, p1, start, end, 70).json()
        r2 = self._create_reservation(park_id, p2, start, end, 70).json()

        barrier = threading.Barrier(2)
        results = {}

        def worker(name, reservation_id):
            client = TestClient(app)
            barrier.wait(timeout=15)
            resp = client.post(
                f"{API}/reservations/confirm",
                json={"reservation_ids": [reservation_id], "operator": name},
            )
            results[name] = resp

        threads = [
            threading.Thread(target=worker, args=("甲", r1["id"])),
            threading.Thread(target=worker, args=("乙", r2["id"])),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(len(results), 2)

        for resp in results.values():
            self.assertEqual(resp.status_code, 200, resp.text)
        statuses = sorted(r.json()["status"] for r in results.values())
        self.assertEqual(statuses, ["confirmed", "rejected"])

        rejected = [r for r in results.values() if r.json()["status"] == "rejected"][0]
        item = rejected.json()["rejections"][0]
        self.assertEqual(item["remaining_amount"], 30)
        self.assertEqual(len(item["sources"]), 1)
        self.assertEqual(item["sources"][0]["amount"], 70)

        availability = self._availability(park_id, start, end)
        for month in months:
            self.assertEqual(availability[month]["committed_amount"], 70)
            self.assertEqual(availability[month]["remaining_amount"], 30)

    # ------------------------------------------------------------------
    # 跨年度期间
    # ------------------------------------------------------------------

    def test_cross_year_period_and_capacity_revision(self):
        as_of = "2026-09-01"
        park_id = self._make_park()
        self.assertEqual(
            self._register_capacity(park_id, 500, (2026, 9), as_of=as_of).status_code,
            200,
        )
        project = self._make_project(park_id)

        # 跨年度预约：2026-11 ~ 2027-02
        resp = self._create_reservation(
            park_id, project, (2026, 11), (2027, 2), 200, as_of=as_of
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        reservation = resp.json()
        self.assertEqual(len(reservation["periods"]), 4)
        self.assertEqual(
            [(p["year"], p["month"]) for p in reservation["periods"]],
            [(2026, 11), (2026, 12), (2027, 1), (2027, 2)],
        )
        resp = self._confirm([reservation["id"]], as_of=as_of)
        self.assertEqual(resp.json()["status"], "confirmed", resp.text)

        availability = self._availability(park_id, (2026, 12), (2027, 1))
        self.assertEqual(availability[(2026, 12)]["committed_amount"], 200)
        self.assertEqual(availability[(2026, 12)]["remaining_amount"], 300)
        self.assertEqual(availability[(2027, 1)]["committed_amount"], 200)

        # 2027 年起容量下调为 300：跨年边界按生效月分别取版本
        self.assertEqual(
            self._register_capacity(
                park_id, 300, (2027, 1), as_of=as_of
            ).status_code,
            200,
        )
        other = self._make_project(park_id)
        r2 = self._create_reservation(
            park_id, other, (2027, 1), (2027, 2), 150, as_of=as_of
        ).json()
        resp = self._confirm([r2["id"]], as_of=as_of)
        body = resp.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(len(body["rejections"]), 2)
        for item in body["rejections"]:
            self.assertEqual(item["year"], 2027)
            self.assertEqual(item["total_capacity"], 300)
            self.assertEqual(item["committed_amount"], 200)
            self.assertEqual(item["remaining_amount"], 100)

        # 2026 年内的月份仍按 500 校验：同年 12 月再约 250 可通过
        r3 = self._create_reservation(
            park_id, other, (2026, 12), (2026, 12), 250, as_of=as_of
        ).json()
        resp = self._confirm([r3["id"]], as_of=as_of)
        self.assertEqual(resp.json()["status"], "confirmed", resp.text)

    # ------------------------------------------------------------------
    # 部分释放
    # ------------------------------------------------------------------

    def test_partial_release_by_month(self):
        park_id = self._make_park()
        months = self._future_months(3)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id)
        reservation = self._create_reservation(park_id, project, start, end, 100).json()
        self.assertEqual(
            self._confirm([reservation["id"]]).json()["status"], "confirmed"
        )

        # 仅释放第 2、3 个月的 40/月
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/release",
            json={
                "amount": 40,
                "start_year": months[1][0],
                "start_month": months[1][1],
                "end_year": months[2][0],
                "end_month": months[2][1],
                "reason": "企业分期建设，暂缓部分用地",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["released"]), 2)
        self.assertEqual(body["skipped_closed_months"], [])
        self.assertEqual(body["reservation"]["status"], "已确认")

        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 100)
        self.assertEqual(availability[months[1]]["committed_amount"], 60)
        self.assertEqual(availability[months[2]]["committed_amount"], 60)

        # 释放量超过剩余额度 → 409
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/release",
            json={"amount": 70, "start_year": months[1][0],
                  "start_month": months[1][1], "end_year": months[1][0],
                  "end_month": months[1][1], "reason": "超额释放"},
        )
        self.assertEqual(resp.status_code, 409)

        # 缺省 amount：释放范围内全部剩余额度，主单关闭
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/release",
            json={"reason": "项目整体调减"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["reservation"]["status"], "已释放")
        availability = self._availability(park_id, start, end)
        for month in months:
            self.assertEqual(availability[month]["committed_amount"], 0)

    # ------------------------------------------------------------------
    # 重复操作
    # ------------------------------------------------------------------

    def test_duplicate_operations_rejected(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id)
        reservation = self._create_reservation(park_id, project, start, end, 100).json()

        # 重复确认
        self.assertEqual(self._confirm([reservation["id"]]).json()["status"], "confirmed")
        resp = self._confirm([reservation["id"]])
        self.assertEqual(resp.status_code, 409)
        self.assertIn("请勿重复确认", resp.json()["detail"])

        # 确认后不可原地修订
        resp = self.client.put(
            f"{API}/reservations/{reservation['id']}", json={"monthly_amount": 50}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("不可原地修订", resp.json()["detail"])

        # 重复释放
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/release",
            json={"reason": "全部释放"},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/release",
            json={"reason": "再次释放"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("不可重复释放", resp.json()["detail"])

        # 重复转移：已转移的预约不能再次转移
        r2 = self._create_reservation(park_id, project, start, end, 60).json()
        self._confirm([r2["id"]])
        target = self._make_project(park_id)
        resp = self.client.post(
            f"{API}/reservations/{r2['id']}/transfer",
            json={"target_project_id": target},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.post(
            f"{API}/reservations/{r2['id']}/transfer",
            json={"target_project_id": target},
        )
        self.assertEqual(resp.status_code, 409)

    # ------------------------------------------------------------------
    # 批量确认原子性
    # ------------------------------------------------------------------

    def test_batch_confirm_is_atomic(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 100, start)
        p1 = self._make_project(park_id)
        p2 = self._make_project(park_id)
        r1 = self._create_reservation(park_id, p1, start, end, 60).json()
        r2 = self._create_reservation(park_id, p2, start, end, 60).json()

        resp = self._confirm([r1["id"], r2["id"]])
        body = resp.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["confirmed"], [])
        # 同批次内的预约也会作为来源承诺列出
        item = body["rejections"][0]
        self.assertEqual(item["committed_amount"], 60)
        self.assertEqual(item["remaining_amount"], 40)
        self.assertEqual(len(item["sources"]), 1)

        # 整批回滚：两张预约都仍是暂存，可用量未被占用
        for rid in (r1["id"], r2["id"]):
            detail = self.client.get(f"{API}/reservations/{rid}").json()
            self.assertEqual(detail["status"], "暂存")
            self.assertEqual(
                [e["event_type"] for e in detail["events"]], ["暂存登记"]
            )
        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 0)

        # 单独确认第一张可通过
        resp = self._confirm([r1["id"]])
        self.assertEqual(resp.json()["status"], "confirmed")

    # ------------------------------------------------------------------
    # 历史月份封存
    # ------------------------------------------------------------------

    def test_closed_months_cannot_be_rewritten(self):
        as_of = "2026-09-10"
        park_id = self._make_park()
        project = self._make_project(park_id)

        # 容量版本不可从历史月份生效
        resp = self._register_capacity(park_id, 100, (2026, 8), as_of=as_of)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("历史月份", resp.json()["detail"])

        # 预约期间不可包含历史月份
        resp = self._create_reservation(
            park_id, project, (2026, 8), (2026, 10), 50, as_of=as_of
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("历史月份", resp.json()["detail"])

        # 当月及未来月份可正常登记确认
        self.assertEqual(
            self._register_capacity(park_id, 100, (2026, 9), as_of=as_of).status_code,
            200,
        )
        resp = self._create_reservation(
            park_id, project, (2026, 9), (2026, 11), 50, as_of=as_of
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        reservation = resp.json()
        resp = self._confirm([reservation["id"]], as_of=as_of)
        self.assertEqual(resp.json()["status"], "confirmed")

        # 时间推进到 11 月：9、10 月已封存，释放只作用于 11 月
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/release",
            json={"reason": "年底盘点释放", "as_of": "2026-11-05"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["released"], [{"year": 2026, "month": 11, "amount": 50}])
        self.assertEqual(
            body["skipped_closed_months"],
            [{"year": 2026, "month": 9}, {"year": 2026, "month": 10}],
        )
        self.assertEqual(body["reservation"]["status"], "已释放")

        # 封存月份的台账行保持原样，未被后来的修订覆盖
        detail = self.client.get(f"{API}/reservations/{reservation['id']}").json()
        periods = {(p["year"], p["month"]): p for p in detail["periods"]}
        self.assertEqual(periods[(2026, 9)]["confirmed_amount"], 50)
        self.assertEqual(periods[(2026, 9)]["released_amount"], 0)
        self.assertEqual(periods[(2026, 10)]["released_amount"], 0)
        self.assertEqual(periods[(2026, 11)]["released_amount"], 50)

        # 封存月份不可确认：上月暂存的预约跨月后确认被拒绝
        stale = self._create_reservation(
            park_id, project, (2026, 10), (2026, 10), 10, as_of=as_of
        ).json()
        resp = self._confirm([stale["id"]], as_of="2026-11-05")
        self.assertEqual(resp.json()["status"], "rejected")
        self.assertIn("封存", resp.json()["rejections"][0]["message"])

    # ------------------------------------------------------------------
    # 阶段变更自动释放
    # ------------------------------------------------------------------

    def test_stage_forward_releases_earlier_stage_reservations(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id, status=ProjectStatus.NEGOTIATING)

        negotiating = self._create_reservation(
            park_id, project, start, end, 100, stage="洽谈中"
        ).json()
        established = self._create_reservation(
            park_id, project, start, end, 80, stage="已立项"
        ).json()
        self._confirm([negotiating["id"], established["id"]])

        # 洽谈中 → 已立项：仅释放「洽谈中」阶段的预约
        resp = self.client.post(
            f"/api/v1/projects/{project}/status",
            json={"to_status": "已立项", "operator": "招商运营", "reason": "完成立项审批"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        detail = self.client.get(f"{API}/reservations/{negotiating['id']}").json()
        self.assertEqual(detail["status"], "已释放")
        auto_events = [e for e in detail["events"] if e["event_type"] == "规则自动释放"]
        self.assertEqual(len(auto_events), 1)
        self.assertEqual(auto_events[0]["detail"]["rule"], "stage_forward")

        detail = self.client.get(f"{API}/reservations/{established['id']}").json()
        self.assertEqual(detail["status"], "已确认")

        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 80)

    def test_stage_backward_releases_later_stage_reservations(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id, status=ProjectStatus.ESTABLISHED)

        established = self._create_reservation(
            park_id, project, start, end, 100, stage="已立项"
        ).json()
        negotiating = self._create_reservation(
            park_id, project, start, end, 60, stage="洽谈中"
        ).json()
        self._confirm([established["id"], negotiating["id"]])

        # 已立项 → 洽谈中（回退需填原因）：释放「已立项」阶段的预约
        resp = self.client.post(
            f"/api/v1/projects/{project}/status",
            json={"to_status": "洽谈中", "reason": "立项条件变化，退回重谈"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        detail = self.client.get(f"{API}/reservations/{established['id']}").json()
        self.assertEqual(detail["status"], "已释放")
        auto_events = [e for e in detail["events"] if e["event_type"] == "规则自动释放"]
        self.assertEqual(auto_events[0]["detail"]["rule"], "stage_backward")

        detail = self.client.get(f"{API}/reservations/{negotiating['id']}").json()
        self.assertEqual(detail["status"], "已确认")

    def test_milestone_progress_triggers_stage_release(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id, status=ProjectStatus.ESTABLISHED)
        reservation = self._create_reservation(
            park_id, project, start, end, 100, stage="已立项"
        ).json()
        self._confirm([reservation["id"]])

        with self.SessionFactory() as db:
            milestone = models.ProjectMilestone(
                project_id=project,
                sequence=1,
                milestone_type=MilestoneType.FOUNDATION,
                name="项目奠基开工",
                status=MilestoneStatus.NOT_STARTED,
                planned_date=date.today(),
            )
            db.add(milestone)
            db.commit()
            milestone_id = milestone.id

        # 里程碑推进 → 项目 已立项→建设中 → 「已立项」阶段预约自动释放
        resp = self.client.put(
            f"/api/v1/workflow/milestones/{milestone_id}",
            json={"status": "进行中"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        detail = self.client.get(f"{API}/reservations/{reservation['id']}").json()
        self.assertEqual(detail["status"], "已释放")
        auto_events = [e for e in detail["events"] if e["event_type"] == "规则自动释放"]
        self.assertEqual(auto_events[0]["detail"]["rule"], "stage_forward")
        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 0)

    def test_approval_triggers_stage_release(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id, status=ProjectStatus.NEGOTIATING)
        reservation = self._create_reservation(
            park_id, project, start, end, 100, stage="洽谈中"
        ).json()
        self._confirm([reservation["id"]])

        # 立项 → 项目 洽谈中→已立项 → 「洽谈中」阶段预约自动释放
        resp = self.client.post(
            f"/api/v1/workflow/projects/{project}/approve",
            json={
                "approval_number": "NNZMQ-2026-0001",
                "approval_date": str(date.today()),
                "approving_authority": "南宁市投资促进局",
                "agreed_investment_10k": 1000,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        detail = self.client.get(f"{API}/reservations/{reservation['id']}").json()
        self.assertEqual(detail["status"], "已释放")
        auto_events = [e for e in detail["events"] if e["event_type"] == "规则自动释放"]
        self.assertEqual(auto_events[0]["detail"]["rule"], "stage_forward")

    def test_project_withdraw_releases_all(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        self._register_capacity(park_id, 800, start, resource="供电")
        project = self._make_project(park_id)

        r1 = self._create_reservation(park_id, project, start, end, 100).json()
        r2 = self._create_reservation(
            park_id, project, start, end, 200, resource="供电"
        ).json()
        self._confirm([r1["id"], r2["id"]])

        resp = self.client.post(
            f"{API}/projects/{project}/withdraw",
            json={"reason": "企业放弃入园"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(
            sorted(body["released_reservation_ids"]), sorted([r1["id"], r2["id"]])
        )

        for rid in (r1["id"], r2["id"]):
            detail = self.client.get(f"{API}/reservations/{rid}").json()
            self.assertEqual(detail["status"], "已释放")
            auto_events = [
                e for e in detail["events"] if e["event_type"] == "规则自动释放"
            ]
            self.assertEqual(auto_events[0]["detail"]["rule"], "project_withdraw")
            self.assertIn("企业放弃入园", auto_events[0]["reason"])

        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 0)
        availability = self._availability(park_id, start, end, resource="供电")
        self.assertEqual(availability[months[0]]["committed_amount"], 0)

    # ------------------------------------------------------------------
    # 转移
    # ------------------------------------------------------------------

    def test_transfer_moves_quota_between_projects(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        source_project = self._make_project(park_id)
        target_project = self._make_project(park_id)
        reservation = self._create_reservation(
            park_id, source_project, start, end, 80
        ).json()
        self._confirm([reservation["id"]])

        # 部分转移 30/月
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/transfer",
            json={"target_project_id": target_project, "amount": 30},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["transferred"]), 2)
        self.assertEqual(body["source"]["status"], "已确认")
        target = body["target"]
        self.assertEqual(target["status"], "已确认")
        self.assertEqual(target["project_id"], target_project)

        source_detail = self.client.get(
            f"{API}/reservations/{reservation['id']}"
        ).json()
        out_events = [e for e in source_detail["events"] if e["event_type"] == "转移转出"]
        self.assertEqual(
            out_events[0]["detail"]["target_reservation_id"], target["id"]
        )
        target_detail = self.client.get(f"{API}/reservations/{target['id']}").json()
        in_events = [e for e in target_detail["events"] if e["event_type"] == "转移转入"]
        self.assertEqual(
            in_events[0]["detail"]["source_reservation_id"], reservation["id"]
        )

        # 园区总占用不变：源 50 + 目标 30 = 80
        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 80)
        sources = availability[months[0]]["sources"]
        self.assertEqual(
            sorted(s["amount"] for s in sources), [30, 50]
        )

        # 跨园区转移被拒绝
        other_park = self._make_park()
        outsider = self._make_project(other_park)
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/transfer",
            json={"target_project_id": outsider, "amount": 10},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("同一园区", resp.json()["detail"])

        # 超额转移被拒绝
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/transfer",
            json={"target_project_id": target_project, "amount": 60},
        )
        self.assertEqual(resp.status_code, 409)

        # 全部剩余转移后，源预约关闭为「已转移」
        resp = self.client.post(
            f"{API}/reservations/{reservation['id']}/transfer",
            json={"target_project_id": target_project},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["source"]["status"], "已转移")
        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[months[0]]["committed_amount"], 80)

    # ------------------------------------------------------------------
    # 暂存修订
    # ------------------------------------------------------------------

    def test_draft_update_and_confirmed_immutable(self):
        park_id = self._make_park()
        months = self._future_months(3)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 500, start)
        project = self._make_project(park_id)
        reservation = self._create_reservation(
            park_id, project, start, months[1], 50
        ).json()

        resp = self.client.put(
            f"{API}/reservations/{reservation['id']}",
            json={
                "monthly_amount": 70,
                "end_year": end[0],
                "end_month": end[1],
                "stage": "已立项",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        detail = resp.json()
        self.assertEqual(len(detail["periods"]), 3)
        self.assertTrue(all(p["requested_amount"] == 70 for p in detail["periods"]))
        self.assertEqual(detail["stage"], "已立项")
        self.assertEqual(
            [e["event_type"] for e in detail["events"]], ["暂存登记", "暂存修订"]
        )

        resp = self._confirm([reservation["id"]])
        self.assertEqual(resp.json()["status"], "confirmed")
        availability = self._availability(park_id, start, end)
        self.assertEqual(availability[end]["committed_amount"], 70)

    # ------------------------------------------------------------------
    # 可用量查询
    # ------------------------------------------------------------------

    def test_availability_endpoint(self):
        park_id = self._make_park()
        months = self._future_months(2)
        start, end = months[0], months[-1]
        self._register_capacity(park_id, 300, start)
        project = self._make_project(park_id)
        reservation = self._create_reservation(park_id, project, start, end, 120).json()
        self._confirm([reservation["id"]])

        availability = self._availability(park_id, start, end)
        month = availability[months[0]]
        self.assertEqual(month["total_capacity"], 300)
        self.assertEqual(month["unit"], "亩")
        self.assertEqual(month["committed_amount"], 120)
        self.assertEqual(month["remaining_amount"], 180)
        self.assertEqual(month["sources"][0]["reservation_code"], reservation["reservation_code"])

        # 未登记容量的资源：总量与剩余为空，占用为 0
        availability = self._availability(park_id, start, end, resource="污水处理")
        month = availability[months[0]]
        self.assertIsNone(month["total_capacity"])
        self.assertIsNone(month["remaining_amount"])
        self.assertEqual(month["committed_amount"], 0)


if __name__ == "__main__":
    unittest.main()
