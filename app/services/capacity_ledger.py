"""园区资源容量预约台账（用地 / 供电 / 污水处理）。

业务规则：
1. 预约按 资源类型 × 有效期间（年月）× 项目阶段 登记，先暂存、后确认；
2. 确认动作在一笔事务内完成总量与既有承诺校验：任一月份超额则整批拒绝，
   并返回每个拒绝项的来源承诺与剩余量；
3. 容量主数据只追加新版本，历史版本不可修改；
4. 已确认的预约不可原地修改，调整只能通过释放 / 转移产生新的台账事件；
5. 历史月份（早于业务基准月）的预约行已封存，不可被后来的修订覆盖；
6. 项目阶段前进释放更早阶段的已确认预约，阶段回退释放更晚阶段的预约，
   项目撤回释放全部已确认预约——均与状态变更在同一事务内提交。

并发控制：所有写路径先执行 :func:`begin_ledger_write`，以 ``ledger_mutex``
单行表的 INSERT OR IGNORE 作为事务首条写语句。SQLite 会把并发写事务
串行化（配合连接的 busy timeout 等待），保证“校验总量与既有承诺”和
“落库”之间不会被其他事务插入新的承诺。
"""

from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, text, tuple_
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas
from ..enums import (
    LedgerEventType,
    ProjectStatus,
    ReservationStatus,
    ResourceType,
)
from ..errors import ERROR_NOT_FOUND
from .status_flow import PROJECT_STATUS_ORDER

_MUTEX_SESSION_KEY = "capacity_ledger_mutex_acquired"
_EPS = 1e-6
_MAX_SPAN_MONTHS = 120

DEFAULT_RESOURCE_UNITS = {
    ResourceType.LAND: "亩",
    ResourceType.POWER: "千瓦",
    ResourceType.WASTEWATER: "吨/日",
}


class LedgerError(ValueError):
    """台账业务规则冲突（路由层映射为 HTTP 409）。"""


class LedgerNotFound(LedgerError):
    """台账对象不存在（路由层映射为 HTTP 404）。"""


# ---------------------------------------------------------------------------
# 月份工具
# ---------------------------------------------------------------------------


def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def iter_months(start_year: int, start_month: int, end_year: int, end_month: int):
    idx = month_index(start_year, start_month)
    last = month_index(end_year, end_month)
    while idx <= last:
        yield idx // 12, idx % 12 + 1
        idx += 1


def resolve_as_of(as_of: Optional[date]) -> date:
    return as_of or date.today()


def is_closed_month(year: int, month: int, as_of: date) -> bool:
    """早于业务基准月的月份视为已封存的历史月份。"""
    return month_index(year, month) < month_index(as_of.year, as_of.month)


# ---------------------------------------------------------------------------
# 写锁
# ---------------------------------------------------------------------------


def begin_ledger_write(db: Session) -> None:
    """在会话事务中抢占台账写锁（幂等）。

    若会话此前只做过读取，先回滚该读事务，保证写锁是事务的首条语句，
    避免 SQLite 共享锁 → 保留锁升级造成的并发死锁。
    仅在写路径入口调用，调用前会话不得有未落库的写入。
    """
    if db.info.get(_MUTEX_SESSION_KEY):
        return
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("begin_ledger_write 必须在任何写入之前调用")
    if db.in_transaction():
        db.rollback()
    db.execute(
        text("INSERT OR IGNORE INTO ledger_mutex (id, note) VALUES (1, '容量台账写锁')")
    )
    db.info[_MUTEX_SESSION_KEY] = True


def _rollback(db: Session) -> None:
    db.rollback()
    db.info.pop(_MUTEX_SESSION_KEY, None)


# ---------------------------------------------------------------------------
# 容量主数据
# ---------------------------------------------------------------------------


def _capacity_revision_for_month(
    db: Session, park_id: int, resource_type: ResourceType, year: int, month: int
) -> Optional[models.ParkResourceCapacity]:
    """取该月份生效的容量版本：生效起始月最晚且不晚于目标月、未过截止月。"""
    rows = (
        db.query(models.ParkResourceCapacity)
        .filter(
            models.ParkResourceCapacity.park_id == park_id,
            models.ParkResourceCapacity.resource_type == resource_type,
        )
        .all()
    )
    target = month_index(year, month)
    best = None
    for row in rows:
        start = month_index(row.effective_from_year, row.effective_from_month)
        if start > target:
            continue
        if row.effective_to_year is not None:
            end = month_index(row.effective_to_year, row.effective_to_month)
            if target > end:
                continue
        if best is None or start > month_index(
            best.effective_from_year, best.effective_from_month
        ):
            best = row
    return best


def register_capacity(
    db: Session, obj_in: schemas.ParkResourceCapacityCreate
) -> models.ParkResourceCapacity:
    begin_ledger_write(db)
    as_of = resolve_as_of(obj_in.as_of)
    park = db.get(models.IndustrialPark, obj_in.park_id)
    if not park:
        raise LedgerNotFound(ERROR_NOT_FOUND["park"])
    if is_closed_month(obj_in.effective_from_year, obj_in.effective_from_month, as_of):
        raise LedgerError("容量登记只能从当前或未来月份生效，历史月份不可修订")
    if (obj_in.effective_to_year is None) != (obj_in.effective_to_month is None):
        raise LedgerError("生效截止年月需同时填写或同时留空")
    if obj_in.effective_to_year is not None and month_index(
        obj_in.effective_to_year, obj_in.effective_to_month
    ) < month_index(obj_in.effective_from_year, obj_in.effective_from_month):
        raise LedgerError("生效截止月不能早于生效起始月")

    duplicate = (
        db.query(models.ParkResourceCapacity)
        .filter(
            models.ParkResourceCapacity.park_id == obj_in.park_id,
            models.ParkResourceCapacity.resource_type == obj_in.resource_type,
            models.ParkResourceCapacity.effective_from_year == obj_in.effective_from_year,
            models.ParkResourceCapacity.effective_from_month == obj_in.effective_from_month,
        )
        .first()
    )
    if duplicate:
        raise LedgerError(
            f"该资源 {obj_in.effective_from_year}年{obj_in.effective_from_month}月 "
            f"已存在容量版本（第 {duplicate.revision} 版），不可重复登记"
        )

    last_revision = (
        db.query(func.max(models.ParkResourceCapacity.revision))
        .filter(
            models.ParkResourceCapacity.park_id == obj_in.park_id,
            models.ParkResourceCapacity.resource_type == obj_in.resource_type,
        )
        .scalar()
    ) or 0

    row = models.ParkResourceCapacity(
        park_id=obj_in.park_id,
        resource_type=obj_in.resource_type,
        total_amount=obj_in.total_amount,
        unit=obj_in.unit or DEFAULT_RESOURCE_UNITS[obj_in.resource_type],
        effective_from_year=obj_in.effective_from_year,
        effective_from_month=obj_in.effective_from_month,
        effective_to_year=obj_in.effective_to_year,
        effective_to_month=obj_in.effective_to_month,
        revision=last_revision + 1,
        note=obj_in.note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_capacities(
    db: Session,
    park_id: Optional[int] = None,
    resource_type: Optional[ResourceType] = None,
) -> List[models.ParkResourceCapacity]:
    query = db.query(models.ParkResourceCapacity)
    if park_id is not None:
        query = query.filter(models.ParkResourceCapacity.park_id == park_id)
    if resource_type is not None:
        query = query.filter(models.ParkResourceCapacity.resource_type == resource_type)
    return query.order_by(
        models.ParkResourceCapacity.park_id,
        models.ParkResourceCapacity.resource_type,
        models.ParkResourceCapacity.effective_from_year,
        models.ParkResourceCapacity.effective_from_month,
    ).all()


# ---------------------------------------------------------------------------
# 占用查询
# ---------------------------------------------------------------------------


def _committed_by_month(
    db: Session,
    park_id: int,
    resource_type: ResourceType,
    months: List[Tuple[int, int]],
) -> Dict[Tuple[int, int], float]:
    """{(year, month): 已确认净占用量}（确认量 − 已释放量）。"""
    if not months:
        return {}
    rows = (
        db.query(
            models.CapacityReservationPeriod.year,
            models.CapacityReservationPeriod.month,
            func.coalesce(
                func.sum(
                    models.CapacityReservationPeriod.confirmed_amount
                    - models.CapacityReservationPeriod.released_amount
                ),
                0.0,
            ),
        )
        .join(
            models.CapacityReservation,
            models.CapacityReservation.id
            == models.CapacityReservationPeriod.reservation_id,
        )
        .filter(
            models.CapacityReservation.park_id == park_id,
            models.CapacityReservation.resource_type == resource_type,
            models.CapacityReservation.status == ReservationStatus.CONFIRMED,
            tuple_(
                models.CapacityReservationPeriod.year,
                models.CapacityReservationPeriod.month,
            ).in_(months),
        )
        .group_by(
            models.CapacityReservationPeriod.year,
            models.CapacityReservationPeriod.month,
        )
        .all()
    )
    return {(year, month): float(total) for year, month, total in rows}


def _occupancy_sources(
    db: Session, park_id: int, resource_type: ResourceType, year: int, month: int
) -> List[dict]:
    """某月的既有承诺来源清单（用于拒绝项溯源与可用量查询）。"""
    rows = (
        db.query(
            models.CapacityReservation,
            models.CapacityReservationPeriod,
            models.Project.name,
        )
        .join(
            models.CapacityReservation,
            models.CapacityReservation.id
            == models.CapacityReservationPeriod.reservation_id,
        )
        .join(models.Project, models.Project.id == models.CapacityReservation.project_id)
        .filter(
            models.CapacityReservation.park_id == park_id,
            models.CapacityReservation.resource_type == resource_type,
            models.CapacityReservation.status == ReservationStatus.CONFIRMED,
            models.CapacityReservationPeriod.year == year,
            models.CapacityReservationPeriod.month == month,
        )
        .order_by(models.CapacityReservation.id)
        .all()
    )
    sources = []
    for reservation, period, project_name in rows:
        remaining = period.remaining_amount
        if remaining <= _EPS:
            continue
        sources.append(
            {
                "reservation_id": reservation.id,
                "reservation_code": reservation.reservation_code,
                "project_id": reservation.project_id,
                "project_name": project_name,
                "stage": reservation.stage,
                "year": year,
                "month": month,
                "amount": remaining,
            }
        )
    return sources


def get_availability(
    db: Session,
    park_id: int,
    resource_type: ResourceType,
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
) -> dict:
    park = db.get(models.IndustrialPark, park_id)
    if not park:
        raise LedgerNotFound(ERROR_NOT_FOUND["park"])
    if month_index(end_year, end_month) < month_index(start_year, start_month):
        raise LedgerError("结束月份不能早于开始月份")
    months = list(iter_months(start_year, start_month, end_year, end_month))
    if len(months) > _MAX_SPAN_MONTHS:
        raise LedgerError(f"查询区间不能超过 {_MAX_SPAN_MONTHS} 个月")
    committed = _committed_by_month(db, park_id, resource_type, months)
    result = []
    for year, month in months:
        cap = _capacity_revision_for_month(db, park_id, resource_type, year, month)
        used = committed.get((year, month), 0.0)
        result.append(
            {
                "year": year,
                "month": month,
                "total_capacity": cap.total_amount if cap else None,
                "unit": cap.unit if cap else None,
                "committed_amount": round(used, 6),
                "remaining_amount": (
                    round(cap.total_amount - used, 6) if cap else None
                ),
                "sources": _occupancy_sources(db, park_id, resource_type, year, month),
            }
        )
    return {"park_id": park_id, "resource_type": resource_type, "months": result}


# ---------------------------------------------------------------------------
# 预约登记（暂存）
# ---------------------------------------------------------------------------


def _validate_span(start_year, start_month, end_year, end_month, as_of: date) -> None:
    if month_index(end_year, end_month) < month_index(start_year, start_month):
        raise LedgerError("结束月份不能早于开始月份")
    span = month_index(end_year, end_month) - month_index(start_year, start_month) + 1
    if span > _MAX_SPAN_MONTHS:
        raise LedgerError(f"预约期间不能超过 {_MAX_SPAN_MONTHS} 个月")
    if is_closed_month(start_year, start_month, as_of):
        raise LedgerError("预约期间包含已封存的历史月份，历史预约不可被修订")


def _record_event(
    db: Session,
    reservation: models.CapacityReservation,
    event_type: LedgerEventType,
    operator: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    db.add(
        models.CapacityLedgerEvent(
            reservation_id=reservation.id,
            event_type=event_type,
            operator=operator,
            reason=reason,
            detail=detail,
            occurred_at=datetime.utcnow(),
        )
    )


def get_reservation(
    db: Session, reservation_id: int
) -> Optional[models.CapacityReservation]:
    return (
        db.query(models.CapacityReservation)
        .options(
            joinedload(models.CapacityReservation.periods),
            joinedload(models.CapacityReservation.events),
        )
        .filter(models.CapacityReservation.id == reservation_id)
        .first()
    )


def _get_reservation_or_404(
    db: Session, reservation_id: int
) -> models.CapacityReservation:
    reservation = get_reservation(db, reservation_id)
    if not reservation:
        raise LedgerNotFound(ERROR_NOT_FOUND["reservation"])
    return reservation


def list_reservations(
    db: Session,
    park_id: Optional[int] = None,
    project_id: Optional[int] = None,
    resource_type: Optional[ResourceType] = None,
    status: Optional[ReservationStatus] = None,
    stage: Optional[ProjectStatus] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[models.CapacityReservation]:
    query = db.query(models.CapacityReservation)
    if park_id is not None:
        query = query.filter(models.CapacityReservation.park_id == park_id)
    if project_id is not None:
        query = query.filter(models.CapacityReservation.project_id == project_id)
    if resource_type is not None:
        query = query.filter(models.CapacityReservation.resource_type == resource_type)
    if status is not None:
        query = query.filter(models.CapacityReservation.status == status)
    if stage is not None:
        query = query.filter(models.CapacityReservation.stage == stage)
    return (
        query.order_by(models.CapacityReservation.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def create_draft(
    db: Session, obj_in: schemas.CapacityReservationCreate
) -> models.CapacityReservation:
    begin_ledger_write(db)
    as_of = resolve_as_of(obj_in.as_of)
    park = db.get(models.IndustrialPark, obj_in.park_id)
    if not park:
        raise LedgerNotFound(ERROR_NOT_FOUND["park"])
    project = db.get(models.Project, obj_in.project_id)
    if not project:
        raise LedgerNotFound(ERROR_NOT_FOUND["project"])
    if project.park_id != obj_in.park_id:
        raise LedgerError("项目不属于该园区，无法在该园区登记容量预约")
    _validate_span(
        obj_in.start_year,
        obj_in.start_month,
        obj_in.end_year,
        obj_in.end_month,
        as_of,
    )

    reservation = models.CapacityReservation(
        park_id=obj_in.park_id,
        project_id=obj_in.project_id,
        resource_type=obj_in.resource_type,
        stage=obj_in.stage,
        status=ReservationStatus.DRAFT,
        start_year=obj_in.start_year,
        start_month=obj_in.start_month,
        end_year=obj_in.end_year,
        end_month=obj_in.end_month,
        remark=obj_in.remark,
        created_by=obj_in.created_by,
    )
    db.add(reservation)
    db.flush()
    reservation.reservation_code = f"RSV-{reservation.id:06d}"
    months = []
    for year, month in iter_months(
        obj_in.start_year, obj_in.start_month, obj_in.end_year, obj_in.end_month
    ):
        db.add(
            models.CapacityReservationPeriod(
                reservation_id=reservation.id,
                year=year,
                month=month,
                requested_amount=obj_in.monthly_amount,
            )
        )
        months.append({"year": year, "month": month, "amount": obj_in.monthly_amount})
    _record_event(
        db,
        reservation,
        LedgerEventType.CREATE_DRAFT,
        operator=obj_in.created_by,
        detail={"months": months},
    )
    db.commit()
    return get_reservation(db, reservation.id)


def update_draft(
    db: Session, reservation_id: int, obj_in: schemas.CapacityReservationUpdate
) -> models.CapacityReservation:
    begin_ledger_write(db)
    reservation = _get_reservation_or_404(db, reservation_id)
    if reservation.status != ReservationStatus.DRAFT:
        raise LedgerError(
            f"预约 {reservation.reservation_code} 已{reservation.status.value}，"
            "台账不可原地修订，请通过释放或转移调整"
        )
    as_of = resolve_as_of(obj_in.as_of)

    start_year = obj_in.start_year if obj_in.start_year is not None else reservation.start_year
    start_month = obj_in.start_month if obj_in.start_month is not None else reservation.start_month
    end_year = obj_in.end_year if obj_in.end_year is not None else reservation.end_year
    end_month = obj_in.end_month if obj_in.end_month is not None else reservation.end_month
    monthly_amount = (
        obj_in.monthly_amount if obj_in.monthly_amount is not None
        else (reservation.periods[0].requested_amount if reservation.periods else None)
    )
    if monthly_amount is None:
        raise LedgerError("预约缺少月度占用量，无法修订")
    _validate_span(start_year, start_month, end_year, end_month, as_of)

    changes = {}
    if obj_in.stage is not None and obj_in.stage != reservation.stage:
        changes["stage"] = {"from": reservation.stage.value, "to": obj_in.stage.value}
        reservation.stage = obj_in.stage
    span_changed = (start_year, start_month, end_year, end_month) != (
        reservation.start_year,
        reservation.start_month,
        reservation.end_year,
        reservation.end_month,
    )
    amount_changed = any(
        abs(p.requested_amount - monthly_amount) > _EPS for p in reservation.periods
    )
    if span_changed:
        changes["span"] = {
            "from": f"{reservation.start_year}-{reservation.start_month:02d} ~ {reservation.end_year}-{reservation.end_month:02d}",
            "to": f"{start_year}-{start_month:02d} ~ {end_year}-{end_month:02d}",
        }
        reservation.start_year = start_year
        reservation.start_month = start_month
        reservation.end_year = end_year
        reservation.end_month = end_month
    if obj_in.remark is not None:
        reservation.remark = obj_in.remark

    if span_changed or amount_changed:
        changes["monthly_amount"] = monthly_amount
        reservation.periods[:] = []
        db.flush()
        for year, month in iter_months(start_year, start_month, end_year, end_month):
            db.add(
                models.CapacityReservationPeriod(
                    reservation_id=reservation.id,
                    year=year,
                    month=month,
                    requested_amount=monthly_amount,
                )
            )

    _record_event(
        db,
        reservation,
        LedgerEventType.UPDATE_DRAFT,
        detail={"changes": changes},
    )
    db.commit()
    return get_reservation(db, reservation.id)


# ---------------------------------------------------------------------------
# 确认（一笔事务内校验总量与既有承诺）
# ---------------------------------------------------------------------------


def _rejection_item(
    reservation: models.CapacityReservation,
    year: int,
    month: int,
    requested: float,
    total: Optional[float],
    committed: float,
    sources: List[dict],
    message: str,
) -> dict:
    remaining = 0.0 if total is None else max(round(total - committed, 6), 0.0)
    return {
        "reservation_id": reservation.id,
        "reservation_code": reservation.reservation_code,
        "resource_type": reservation.resource_type,
        "year": year,
        "month": month,
        "requested_amount": requested,
        "total_capacity": total,
        "committed_amount": round(committed, 6),
        "remaining_amount": remaining,
        "message": message,
        "sources": sources,
    }


def confirm_reservations(db: Session, obj_in: schemas.ConfirmRequest) -> dict:
    """批量确认暂存预约：任一月份超额则整批拒绝，不落下任何部分写入。"""
    begin_ledger_write(db)
    as_of = resolve_as_of(obj_in.as_of)

    if len(set(obj_in.reservation_ids)) != len(obj_in.reservation_ids):
        raise LedgerError("请求中包含重复的预约ID")

    reservations = []
    for reservation_id in obj_in.reservation_ids:
        reservation = db.get(models.CapacityReservation, reservation_id)
        if not reservation:
            raise LedgerNotFound(f"容量预约 {reservation_id} 不存在")
        if reservation.status != ReservationStatus.DRAFT:
            raise LedgerError(
                f"预约 {reservation.reservation_code} 当前状态为"
                f"「{reservation.status.value}」，仅「暂存」状态可确认，请勿重复确认"
            )
        reservations.append(reservation)

    committed_cache: Dict[tuple, Dict[Tuple[int, int], float]] = {}
    for reservation in reservations:
        key = (reservation.park_id, reservation.resource_type)
        if key in committed_cache:
            continue
        months = [
            (p.year, p.month)
            for other in reservations
            if (other.park_id, other.resource_type) == key
            for p in other.periods
        ]
        committed_cache[key] = _committed_by_month(
            db, reservation.park_id, reservation.resource_type, months
        )

    rejections: List[dict] = []
    pending: Dict[tuple, float] = defaultdict(float)
    pending_sources: Dict[tuple, List[dict]] = defaultdict(list)
    for reservation in reservations:
        key = (reservation.park_id, reservation.resource_type)
        committed_map = committed_cache[key]
        project_name = reservation.project.name if reservation.project else None
        for period in reservation.periods:
            year, month = period.year, period.month
            pkey = (reservation.park_id, reservation.resource_type, year, month)
            if is_closed_month(year, month, as_of):
                rejections.append(
                    _rejection_item(
                        reservation, year, month, period.requested_amount,
                        None, 0.0, [], "历史月份已封存，不可确认",
                    )
                )
                continue
            cap = _capacity_revision_for_month(
                db, reservation.park_id, reservation.resource_type, year, month
            )
            already = committed_map.get((year, month), 0.0) + pending[pkey]
            if cap is None:
                rejections.append(
                    _rejection_item(
                        reservation, year, month, period.requested_amount,
                        None, already, [],
                        "园区未登记该资源在该月份的容量上限",
                    )
                )
                continue
            if already + period.requested_amount > cap.total_amount + _EPS:
                sources = _occupancy_sources(
                    db, reservation.park_id, reservation.resource_type, year, month
                ) + list(pending_sources[pkey])
                rejections.append(
                    _rejection_item(
                        reservation, year, month, period.requested_amount,
                        cap.total_amount, already, sources, "容量不足",
                    )
                )
                continue
            pending[pkey] += period.requested_amount
            pending_sources[pkey].append(
                {
                    "reservation_id": reservation.id,
                    "reservation_code": reservation.reservation_code,
                    "project_id": reservation.project_id,
                    "project_name": project_name,
                    "stage": reservation.stage,
                    "year": year,
                    "month": month,
                    "amount": period.requested_amount,
                }
            )

    if rejections:
        _rollback(db)
        return {"status": "rejected", "confirmed": [], "rejections": rejections}

    now = datetime.utcnow()
    for reservation in reservations:
        reservation.status = ReservationStatus.CONFIRMED
        reservation.confirmed_at = now
        months = []
        for period in reservation.periods:
            period.confirmed_amount = period.requested_amount
            months.append(
                {
                    "year": period.year,
                    "month": period.month,
                    "amount": period.requested_amount,
                }
            )
        _record_event(
            db,
            reservation,
            LedgerEventType.CONFIRM,
            operator=obj_in.operator,
            detail={"months": months, "batch": obj_in.reservation_ids},
        )
    db.commit()
    confirmed = [get_reservation(db, r.id) for r in reservations]
    return {"status": "confirmed", "confirmed": confirmed, "rejections": []}


# ---------------------------------------------------------------------------
# 释放 / 转移 / 撤回
# ---------------------------------------------------------------------------


def _close_if_drained(
    db: Session,
    reservation: models.CapacityReservation,
    as_of: date,
    final_status: ReservationStatus,
) -> None:
    """所有未封存月份额度归零时关闭主单；封存月份的留存作为历史档案保留。"""
    for period in reservation.periods:
        if is_closed_month(period.year, period.month, as_of):
            continue
        if period.remaining_amount > _EPS:
            return
    reservation.status = final_status
    reservation.closed_at = datetime.utcnow()


def _release_open_months(
    db: Session, reservation: models.CapacityReservation, as_of: date
) -> List[dict]:
    """释放全部未封存月份的剩余额度，返回逐月释放明细。"""
    released = []
    for period in reservation.periods:
        if is_closed_month(period.year, period.month, as_of):
            continue
        remaining = period.remaining_amount
        if remaining <= _EPS:
            continue
        period.released_amount = period.confirmed_amount
        released.append(
            {"year": period.year, "month": period.month, "amount": round(remaining, 6)}
        )
    return released


def _resolve_scope(reservation, obj_in) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    start = (
        (obj_in.start_year, obj_in.start_month)
        if obj_in.start_year is not None and obj_in.start_month is not None
        else (reservation.start_year, reservation.start_month)
    )
    end = (
        (obj_in.end_year, obj_in.end_month)
        if obj_in.end_year is not None and obj_in.end_month is not None
        else (reservation.end_year, reservation.end_month)
    )
    if month_index(*end) < month_index(*start):
        raise LedgerError("期间起止颠倒：结束月份不能早于开始月份")
    return start, end


def release_reservation(
    db: Session, reservation_id: int, obj_in: schemas.ReleaseRequest
) -> dict:
    begin_ledger_write(db)
    reservation = _get_reservation_or_404(db, reservation_id)
    as_of = resolve_as_of(obj_in.as_of)

    if reservation.status == ReservationStatus.DRAFT:
        reservation.status = ReservationStatus.RELEASED
        reservation.closed_at = datetime.utcnow()
        _record_event(
            db,
            reservation,
            LedgerEventType.RELEASE,
            operator=obj_in.operator,
            reason=obj_in.reason,
            detail={"scope": "暂存作废"},
        )
        db.commit()
        return {
            "reservation": get_reservation(db, reservation.id),
            "released": [],
            "skipped_closed_months": [],
        }
    if reservation.status != ReservationStatus.CONFIRMED:
        raise LedgerError(
            f"预约 {reservation.reservation_code} 当前状态为"
            f"「{reservation.status.value}」，不可重复释放"
        )

    start, end = _resolve_scope(reservation, obj_in)

    # 第一遍：收集范围并校验，不做任何修改
    targets = []
    skipped = []
    for period in reservation.periods:
        if not (month_index(*start) <= month_index(period.year, period.month) <= month_index(*end)):
            continue
        if is_closed_month(period.year, period.month, as_of):
            skipped.append({"year": period.year, "month": period.month})
            continue
        remaining = period.remaining_amount
        if remaining <= _EPS:
            continue
        amount = remaining if obj_in.amount is None else obj_in.amount
        if amount > remaining + _EPS:
            raise LedgerError(
                f"{period.year}年{period.month}月剩余额度 {remaining}，"
                f"无法释放 {obj_in.amount}"
            )
        targets.append((period, amount))

    if not targets:
        message = "所选期间没有可释放的剩余额度"
        if skipped:
            message += "（历史月份已封存，不可修订）"
        raise LedgerError(message)

    # 第二遍：统一落库
    released = []
    for period, amount in targets:
        period.released_amount = round(period.released_amount + amount, 6)
        released.append(
            {"year": period.year, "month": period.month, "amount": round(amount, 6)}
        )

    _record_event(
        db,
        reservation,
        LedgerEventType.RELEASE,
        operator=obj_in.operator,
        reason=obj_in.reason,
        detail={"months": released, "skipped_closed_months": skipped},
    )
    _close_if_drained(db, reservation, as_of, ReservationStatus.RELEASED)
    db.commit()
    return {
        "reservation": get_reservation(db, reservation.id),
        "released": released,
        "skipped_closed_months": skipped,
    }


def transfer_reservation(
    db: Session, reservation_id: int, obj_in: schemas.TransferRequest
) -> dict:
    begin_ledger_write(db)
    source = _get_reservation_or_404(db, reservation_id)
    as_of = resolve_as_of(obj_in.as_of)

    if source.status != ReservationStatus.CONFIRMED:
        raise LedgerError(
            f"预约 {source.reservation_code} 当前状态为「{source.status.value}」，"
            "仅「已确认」的预约可转移"
        )
    if obj_in.target_project_id == source.project_id:
        raise LedgerError("目标项目不能是原项目自身")
    target_project = db.get(models.Project, obj_in.target_project_id)
    if not target_project:
        raise LedgerNotFound(ERROR_NOT_FOUND["project"])
    if target_project.park_id != source.park_id:
        raise LedgerError("仅支持同一园区内的项目间转移")

    start, end = _resolve_scope(source, obj_in)

    scope = []
    skipped = []
    for period in source.periods:
        if not (month_index(*start) <= month_index(period.year, period.month) <= month_index(*end)):
            continue
        if is_closed_month(period.year, period.month, as_of):
            skipped.append({"year": period.year, "month": period.month})
            continue
        remaining = period.remaining_amount
        if remaining <= _EPS:
            continue
        amount = remaining if obj_in.amount is None else obj_in.amount
        if amount > remaining + _EPS:
            raise LedgerError(
                f"{period.year}年{period.month}月剩余额度 {remaining}，"
                f"无法转移 {obj_in.amount}"
            )
        scope.append((period, amount))

    if not scope:
        message = "所选期间没有可转移的剩余额度"
        if skipped:
            message += "（历史月份已封存，不可修订）"
        raise LedgerError(message)

    target = models.CapacityReservation(
        park_id=source.park_id,
        project_id=target_project.id,
        resource_type=source.resource_type,
        stage=source.stage,
        status=ReservationStatus.CONFIRMED,
        start_year=scope[0][0].year,
        start_month=scope[0][0].month,
        end_year=scope[-1][0].year,
        end_month=scope[-1][0].month,
        remark=obj_in.remark or f"由预约 {source.reservation_code} 转入",
        created_by=obj_in.operator,
        confirmed_at=datetime.utcnow(),
    )
    db.add(target)
    db.flush()
    target.reservation_code = f"RSV-{target.id:06d}"

    transferred = []
    for period, amount in scope:
        period.released_amount = round(period.released_amount + amount, 6)
        db.add(
            models.CapacityReservationPeriod(
                reservation_id=target.id,
                year=period.year,
                month=period.month,
                requested_amount=amount,
                confirmed_amount=amount,
            )
        )
        transferred.append(
            {"year": period.year, "month": period.month, "amount": round(amount, 6)}
        )
    db.flush()

    # 防御性校验：转移后目标预约逐月占用不得超过园区容量上限。
    # 同园区同资源下源预约已在本事务内释放，正常必通过；异常时整体回滚。
    violations = []
    for period in target.periods:
        cap = _capacity_revision_for_month(
            db, target.park_id, target.resource_type, period.year, period.month
        )
        committed = _committed_by_month(
            db, target.park_id, target.resource_type, [(period.year, period.month)]
        ).get((period.year, period.month), 0.0)
        if cap is None or committed > cap.total_amount + _EPS:
            violations.append((period.year, period.month))
    if violations:
        _rollback(db)
        raise LedgerError("转移后超出园区容量上限，操作已回滚")

    _record_event(
        db,
        source,
        LedgerEventType.TRANSFER_OUT,
        operator=obj_in.operator,
        detail={
            "target_reservation_id": target.id,
            "target_reservation_code": target.reservation_code,
            "target_project_id": target_project.id,
            "months": transferred,
            "skipped_closed_months": skipped,
        },
    )
    _record_event(
        db,
        target,
        LedgerEventType.TRANSFER_IN,
        operator=obj_in.operator,
        detail={
            "source_reservation_id": source.id,
            "source_reservation_code": source.reservation_code,
            "source_project_id": source.project_id,
            "months": transferred,
        },
    )
    _close_if_drained(db, source, as_of, ReservationStatus.TRANSFERRED)
    db.commit()
    return {
        "source": get_reservation(db, source.id),
        "target": get_reservation(db, target.id),
        "transferred": transferred,
        "skipped_closed_months": skipped,
    }


def withdraw_project_reservations(
    db: Session, project_id: int, obj_in: schemas.WithdrawRequest
) -> dict:
    """项目撤回：释放该项目全部已确认预约的未使用额度。"""
    begin_ledger_write(db)
    project = db.get(models.Project, project_id)
    if not project:
        raise LedgerNotFound(ERROR_NOT_FOUND["project"])
    as_of = resolve_as_of(obj_in.as_of)

    reservations = (
        db.query(models.CapacityReservation)
        .options(joinedload(models.CapacityReservation.periods))
        .filter(
            models.CapacityReservation.project_id == project_id,
            models.CapacityReservation.status == ReservationStatus.CONFIRMED,
        )
        .all()
    )
    affected = []
    for reservation in reservations:
        released = _release_open_months(db, reservation, as_of)
        if not released:
            continue
        _record_event(
            db,
            reservation,
            LedgerEventType.AUTO_RELEASE,
            operator=obj_in.operator,
            reason=f"项目撤回：{obj_in.reason}",
            detail={"rule": "project_withdraw", "months": released},
        )
        _close_if_drained(db, reservation, as_of, ReservationStatus.RELEASED)
        affected.append(reservation.id)
    db.commit()
    return {
        "project_id": project_id,
        "released_reservation_ids": affected,
        "reservations": [get_reservation(db, rid) for rid in affected],
    }


def apply_stage_change_release(
    db: Session,
    project: models.Project,
    from_stage: ProjectStatus,
    to_stage: ProjectStatus,
    operator: Optional[str] = None,
    as_of: Optional[date] = None,
) -> List[int]:
    """项目阶段变更的自动释放规则（与状态变更在同一事务内，由调用方提交）。

    阶段前进：释放更早阶段的已确认预约；阶段回退：释放更晚阶段的预约。
    """
    if from_stage == to_stage:
        return []
    order = PROJECT_STATUS_ORDER
    from_idx = order.index(from_stage)
    to_idx = order.index(to_stage)
    if to_idx > from_idx:
        doomed_stages = order[:to_idx]
        rule = "stage_forward"
        direction = "前进"
    else:
        doomed_stages = order[to_idx + 1 :]
        rule = "stage_backward"
        direction = "回退"

    as_of_date = resolve_as_of(as_of)
    reservations = (
        db.query(models.CapacityReservation)
        .options(joinedload(models.CapacityReservation.periods))
        .filter(
            models.CapacityReservation.project_id == project.id,
            models.CapacityReservation.status == ReservationStatus.CONFIRMED,
            models.CapacityReservation.stage.in_(doomed_stages),
        )
        .all()
    )
    affected = []
    for reservation in reservations:
        released = _release_open_months(db, reservation, as_of_date)
        if not released:
            continue
        _record_event(
            db,
            reservation,
            LedgerEventType.AUTO_RELEASE,
            operator=operator,
            reason=(
                f"项目阶段{direction}（{from_stage.value} → {to_stage.value}），"
                f"按规则释放「{reservation.stage.value}」阶段未使用额度"
            ),
            detail={
                "rule": rule,
                "from_stage": from_stage.value,
                "to_stage": to_stage.value,
                "months": released,
            },
        )
        _close_if_drained(db, reservation, as_of_date, ReservationStatus.RELEASED)
        affected.append(reservation.id)
    return affected
