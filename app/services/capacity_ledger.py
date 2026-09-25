"""园区资源容量预约台账。

核心规则：
- 预约按「资源类型 + 有效期间（年月粒度）+ 项目阶段」登记，先暂存、后确认；
- 确认在一笔事务内完成：先对容量池行加写锁（SQLite 下串行化并发确认），
  再校验总量与既有承诺，任一月份不足即整体回滚并返回每个拒绝月份的
  来源承诺与剩余量；
- 确认后按月份落台账明细，历史月份的明细不可被后续修订/释放/转移覆盖，
  修订只重写当月及以后的明细；
- 释放支持部分释放（按生效月份缩减未来明细），转移在同园区同资源内
  把未来月份额度过户给其他项目；
- 项目撤回（退回「招商中」）或阶段回退时，按规则释放未使用额度；
- 每个操作写事件留痕，幂等键保证重复操作不重复入账。
"""

import json
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas
from ..enums import (
    ProjectStatus,
    ReservationEventType,
    ReservationStatus,
    ResourceType,
    RESOURCE_TYPE_DEFAULT_UNIT,
)
from ..errors import ERROR_NOT_FOUND, ERROR_RESERVATION, fmt
from .status_flow import PROJECT_STATUS_ORDER

_EPS = 1e-6


# ---------- 异常 ----------


class CapacityConflictError(Exception):
    """容量不足：携带每个拒绝月份的来源承诺与剩余量。"""

    def __init__(self, rejections: List[dict]):
        self.rejections = rejections
        super().__init__(ERROR_RESERVATION["insufficient"])


class ReservationStateError(ValueError):
    """预约状态或参数不满足业务规则。"""


# ---------- 月份工具 ----------


def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def iter_months(start_year: int, start_month: int, end_year: int, end_month: int):
    idx = month_index(start_year, start_month)
    end = month_index(end_year, end_month)
    while idx <= end:
        yield idx // 12, idx % 12 + 1
        idx += 1


def current_month(today: Optional[date] = None) -> Tuple[int, int]:
    today = today or date.today()
    return today.year, today.month


def _future_months(
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
    today: Optional[date] = None,
) -> List[Tuple[int, int]]:
    """期间内当月及以后的月份（历史月份不落账、不可改）。"""
    cur = month_index(*current_month(today))
    return [
        (y, m)
        for (y, m) in iter_months(start_year, start_month, end_year, end_month)
        if month_index(y, m) >= cur
    ]


# ---------- 容量池 ----------


def _get_pool(
    db: Session, park_id: int, resource_type: ResourceType
) -> Optional[models.ParkResourceCapacity]:
    return (
        db.query(models.ParkResourceCapacity)
        .filter(
            models.ParkResourceCapacity.park_id == park_id,
            models.ParkResourceCapacity.resource_type == resource_type,
        )
        .first()
    )


def upsert_pool(
    db: Session, obj_in: schemas.CapacityPoolCreate
) -> models.ParkResourceCapacity:
    unit = obj_in.unit or RESOURCE_TYPE_DEFAULT_UNIT[obj_in.resource_type]
    pool = _get_pool(db, obj_in.park_id, obj_in.resource_type)
    if pool:
        pool.total_amount = obj_in.total_amount
        pool.unit = unit
        pool.remark = obj_in.remark
    else:
        pool = models.ParkResourceCapacity(
            park_id=obj_in.park_id,
            resource_type=obj_in.resource_type,
            total_amount=obj_in.total_amount,
            unit=unit,
            remark=obj_in.remark,
        )
        db.add(pool)
    db.commit()
    db.refresh(pool)
    return pool


def list_pools(
    db: Session, park_id: Optional[int] = None
) -> List[models.ParkResourceCapacity]:
    query = db.query(models.ParkResourceCapacity)
    if park_id:
        query = query.filter(models.ParkResourceCapacity.park_id == park_id)
    return query.order_by(models.ParkResourceCapacity.id).all()


def _lock_pool(
    db: Session, park_id: int, resource_type: ResourceType, required: bool = True
) -> Optional[models.ParkResourceCapacity]:
    """在同一事务内对容量池行加写锁。

    SQLite 下写锁串行化并发事务：并发的确认/修订/转移在此排队，
    保证「校验总量 + 落账」不被其他承诺插入打断。
    """
    pool = _get_pool(db, park_id, resource_type)
    if not pool:
        if required:
            raise ReservationStateError(
                fmt(
                    ERROR_RESERVATION["pool_missing"],
                    resource_type=resource_type.value,
                )
            )
        return None
    db.execute(
        update(models.ParkResourceCapacity)
        .where(models.ParkResourceCapacity.id == pool.id)
        .values(lock_version=models.ParkResourceCapacity.lock_version + 1)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    return db.get(models.ParkResourceCapacity, pool.id, populate_existing=True)


# ---------- 占用与来源查询 ----------


def _month_idx_expr():
    # 与 month_index() 保持一致：year * 12 + (month - 1)
    return (
        models.CapacityReservationEntry.year * 12
        + models.CapacityReservationEntry.month
        - 1
    )


def _occupied_by_month(
    db: Session,
    park_id: int,
    resource_type: ResourceType,
    months: List[Tuple[int, int]],
    exclude_reservation_id: Optional[int] = None,
) -> Dict[Tuple[int, int], float]:
    if not months:
        return {}
    lo = month_index(*months[0])
    hi = month_index(*months[-1])
    idx = _month_idx_expr()
    query = db.query(
        models.CapacityReservationEntry.year,
        models.CapacityReservationEntry.month,
        func.sum(models.CapacityReservationEntry.amount),
    ).filter(
        models.CapacityReservationEntry.park_id == park_id,
        models.CapacityReservationEntry.resource_type == resource_type,
        and_(idx >= lo, idx <= hi),
    )
    if exclude_reservation_id:
        query = query.filter(
            models.CapacityReservationEntry.reservation_id != exclude_reservation_id
        )
    query = query.group_by(
        models.CapacityReservationEntry.year, models.CapacityReservationEntry.month
    )
    return {(y, m): float(total or 0.0) for y, m, total in query.all()}


def _sources_by_month(
    db: Session,
    park_id: int,
    resource_type: ResourceType,
    months: List[Tuple[int, int]],
    exclude_reservation_id: Optional[int] = None,
) -> Dict[Tuple[int, int], List[dict]]:
    if not months:
        return {}
    lo = month_index(*months[0])
    hi = month_index(*months[-1])
    idx = _month_idx_expr()
    query = (
        db.query(
            models.CapacityReservationEntry.year,
            models.CapacityReservationEntry.month,
            models.CapacityReservationEntry.amount,
            models.CapacityReservation,
            models.Project.name.label("project_name"),
        )
        .join(
            models.CapacityReservation,
            models.CapacityReservationEntry.reservation_id
            == models.CapacityReservation.id,
        )
        .outerjoin(
            models.Project, models.CapacityReservation.project_id == models.Project.id
        )
        .filter(
            models.CapacityReservationEntry.park_id == park_id,
            models.CapacityReservationEntry.resource_type == resource_type,
            and_(idx >= lo, idx <= hi),
        )
    )
    if exclude_reservation_id:
        query = query.filter(
            models.CapacityReservationEntry.reservation_id != exclude_reservation_id
        )
    sources: Dict[Tuple[int, int], List[dict]] = {}
    for y, m, amount, reservation, project_name in query.all():
        sources.setdefault((y, m), []).append(
            {
                "reservation_id": reservation.id,
                "reservation_no": reservation.reservation_no,
                "project_id": reservation.project_id,
                "project_name": project_name,
                "stage": reservation.stage.value,
                "status": reservation.status.value,
                "amount": float(amount),
            }
        )
    return sources


def _validate_capacity(
    db: Session,
    pool: models.ParkResourceCapacity,
    months: List[Tuple[int, int]],
    amount: float,
    exclude_reservation_id: Optional[int] = None,
) -> None:
    """校验期间内每个月的既有承诺 + 本次申请不超过总量，否则抛出拒绝明细。"""
    occupied = _occupied_by_month(
        db,
        pool.park_id,
        pool.resource_type,
        months,
        exclude_reservation_id=exclude_reservation_id,
    )
    lacking = [
        (y, m)
        for (y, m) in months
        if pool.total_amount - occupied.get((y, m), 0.0) < amount - _EPS
    ]
    if not lacking:
        return
    sources = _sources_by_month(
        db,
        pool.park_id,
        pool.resource_type,
        lacking,
        exclude_reservation_id=exclude_reservation_id,
    )
    rejections = [
        {
            "year": y,
            "month": m,
            "requested": amount,
            "capacity": pool.total_amount,
            "occupied": round(occupied.get((y, m), 0.0), 4),
            "remaining": round(pool.total_amount - occupied.get((y, m), 0.0), 4),
            "sources": sources.get((y, m), []),
        }
        for (y, m) in lacking
    ]
    raise CapacityConflictError(rejections)


def get_availability(
    db: Session,
    park_id: int,
    resource_type: ResourceType,
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
) -> Optional[dict]:
    """逐月返回容量、已占用、剩余量及占用来源，供新项目录入时核对。"""
    pool = _get_pool(db, park_id, resource_type)
    if not pool:
        return None
    months = list(iter_months(start_year, start_month, end_year, end_month))
    occupied = _occupied_by_month(db, park_id, resource_type, months)
    sources = _sources_by_month(db, park_id, resource_type, months)
    return {
        "park_id": park_id,
        "resource_type": resource_type,
        "unit": pool.unit,
        "total_amount": pool.total_amount,
        "months": [
            {
                "year": y,
                "month": m,
                "capacity": pool.total_amount,
                "occupied": round(occupied.get((y, m), 0.0), 4),
                "remaining": round(pool.total_amount - occupied.get((y, m), 0.0), 4),
                "sources": sources.get((y, m), []),
            }
            for (y, m) in months
        ],
    }


# ---------- 台账明细 ----------


def _delete_entries_from(db: Session, reservation_id: int, from_index: int) -> int:
    """删除指定月份起的明细（只允许删除当月及以后，历史月份不动）。"""
    return (
        db.query(models.CapacityReservationEntry)
        .filter(
            models.CapacityReservationEntry.reservation_id == reservation_id,
            _month_idx_expr() >= from_index,
        )
        .delete(synchronize_session=False)
    )


def _rewrite_entries_from(
    db: Session, reservation_id: int, from_index: int, new_amount: float
) -> int:
    """把指定月份起的明细改为新额度（历史月份保持不变）。"""
    return (
        db.query(models.CapacityReservationEntry)
        .filter(
            models.CapacityReservationEntry.reservation_id == reservation_id,
            _month_idx_expr() >= from_index,
        )
        .update({"amount": new_amount}, synchronize_session=False)
    )


def _insert_entries(
    db: Session,
    reservation: models.CapacityReservation,
    months: List[Tuple[int, int]],
    amount: float,
) -> None:
    for (y, m) in months:
        db.add(
            models.CapacityReservationEntry(
                reservation_id=reservation.id,
                park_id=reservation.park_id,
                resource_type=reservation.resource_type,
                year=y,
                month=m,
                amount=amount,
            )
        )


# ---------- 事件与幂等 ----------


def _add_event(
    db: Session,
    reservation_id: int,
    event_type: ReservationEventType,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
) -> models.CapacityReservationEvent:
    event = models.CapacityReservationEvent(
        reservation_id=reservation_id,
        event_type=event_type,
        actor=actor,
        reason=reason,
        payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    return event


def _find_event_by_key(
    db: Session, idempotency_key: Optional[str]
) -> Optional[models.CapacityReservationEvent]:
    if not idempotency_key:
        return None
    return (
        db.query(models.CapacityReservationEvent)
        .filter(models.CapacityReservationEvent.idempotency_key == idempotency_key)
        .first()
    )


def _snapshot(reservation: models.CapacityReservation) -> dict:
    return {
        "status": reservation.status.value,
        "stage": reservation.stage.value,
        "amount": reservation.amount,
        "released_amount": reservation.released_amount,
        "period": (
            f"{reservation.start_year}-{reservation.start_month:02d}"
            f" ~ {reservation.end_year}-{reservation.end_month:02d}"
        ),
    }


# ---------- 查询 ----------


def get_reservation(
    db: Session, reservation_id: int
) -> Optional[models.CapacityReservation]:
    return (
        db.query(models.CapacityReservation)
        .options(
            joinedload(models.CapacityReservation.entries),
            joinedload(models.CapacityReservation.events),
        )
        .filter(models.CapacityReservation.id == reservation_id)
        .first()
    )


def list_reservations(
    db: Session,
    park_id: Optional[int] = None,
    project_id: Optional[int] = None,
    resource_type: Optional[ResourceType] = None,
    status: Optional[ReservationStatus] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[models.CapacityReservation]:
    query = db.query(models.CapacityReservation)
    if park_id:
        query = query.filter(models.CapacityReservation.park_id == park_id)
    if project_id:
        query = query.filter(models.CapacityReservation.project_id == project_id)
    if resource_type:
        query = query.filter(models.CapacityReservation.resource_type == resource_type)
    if status:
        query = query.filter(models.CapacityReservation.status == status)
    return (
        query.order_by(models.CapacityReservation.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


# ---------- 暂存 ----------


def create_reservation(
    db: Session, obj_in: schemas.CapacityReservationCreate
) -> models.CapacityReservation:
    if obj_in.idempotency_key:
        existing = (
            db.query(models.CapacityReservation)
            .filter(
                models.CapacityReservation.idempotency_key == obj_in.idempotency_key
            )
            .first()
        )
        if existing:
            return get_reservation(db, existing.id)

    project = db.get(models.Project, obj_in.project_id)
    if not project:
        raise ReservationStateError(ERROR_NOT_FOUND["project"])
    if project.park_id != obj_in.park_id:
        raise ReservationStateError(ERROR_RESERVATION["project_park_mismatch"])

    pool = _get_pool(db, obj_in.park_id, obj_in.resource_type)
    unit = pool.unit if pool else RESOURCE_TYPE_DEFAULT_UNIT[obj_in.resource_type]
    stage = obj_in.stage or project.status

    reservation = models.CapacityReservation(
        park_id=obj_in.park_id,
        project_id=obj_in.project_id,
        resource_type=obj_in.resource_type,
        stage=stage,
        status=ReservationStatus.DRAFT,
        amount=obj_in.amount,
        unit=unit,
        start_year=obj_in.start_year,
        start_month=obj_in.start_month,
        end_year=obj_in.end_year,
        end_month=obj_in.end_month,
        idempotency_key=obj_in.idempotency_key,
        operator=obj_in.operator,
        remark=obj_in.remark,
    )
    db.add(reservation)
    db.flush()
    reservation.reservation_no = (
        f"RSV{datetime.utcnow():%Y%m%d}-{reservation.id:05d}"
    )
    _add_event(
        db,
        reservation.id,
        ReservationEventType.CREATED,
        actor=obj_in.operator,
        payload=_snapshot(reservation),
        idempotency_key=obj_in.idempotency_key,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if obj_in.idempotency_key:
            existing = (
                db.query(models.CapacityReservation)
                .filter(
                    models.CapacityReservation.idempotency_key
                    == obj_in.idempotency_key
                )
                .first()
            )
            if existing:
                return get_reservation(db, existing.id)
        raise
    return get_reservation(db, reservation.id)


# ---------- 确认 ----------


def confirm_reservation(
    db: Session,
    reservation_id: int,
    operator: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    today: Optional[date] = None,
) -> Optional[models.CapacityReservation]:
    replay = _find_event_by_key(db, idempotency_key)
    if (
        replay
        and replay.reservation_id == reservation_id
        and replay.event_type == ReservationEventType.CONFIRMED
    ):
        return get_reservation(db, reservation_id)

    reservation = db.get(models.CapacityReservation, reservation_id)
    if not reservation:
        return None
    # 一笔事务内：锁容量池 → 重读预约 → 校验总量与既有承诺 → 落账
    pool = _lock_pool(db, reservation.park_id, reservation.resource_type)
    reservation = db.get(
        models.CapacityReservation, reservation_id, populate_existing=True
    )
    if reservation.status == ReservationStatus.CONFIRMED:
        return get_reservation(db, reservation_id)  # 重复确认，幂等返回
    if reservation.status != ReservationStatus.DRAFT:
        raise ReservationStateError(
            fmt(
                ERROR_RESERVATION["invalid_status"],
                status=reservation.status.value,
            )
        )

    months = _future_months(
        reservation.start_year,
        reservation.start_month,
        reservation.end_year,
        reservation.end_month,
        today,
    )
    if not months:
        raise ReservationStateError(ERROR_RESERVATION["period_fully_past"])

    _validate_capacity(
        db, pool, months, reservation.amount, exclude_reservation_id=reservation.id
    )
    _delete_entries_from(db, reservation.id, month_index(*current_month(today)))
    _insert_entries(db, reservation, months, reservation.amount)
    reservation.status = ReservationStatus.CONFIRMED
    reservation.confirmed_at = datetime.utcnow()
    _add_event(
        db,
        reservation.id,
        ReservationEventType.CONFIRMED,
        actor=operator,
        payload={"amount": reservation.amount, "months": len(months)},
        idempotency_key=idempotency_key,
    )
    db.commit()
    return get_reservation(db, reservation_id)


# ---------- 修订 ----------


def revise_reservation(
    db: Session,
    reservation_id: int,
    obj_in: schemas.CapacityReservationUpdate,
    today: Optional[date] = None,
) -> Optional[models.CapacityReservation]:
    reservation = db.get(models.CapacityReservation, reservation_id)
    if not reservation:
        return None
    if reservation.status in (ReservationStatus.RELEASED, ReservationStatus.TRANSFERRED):
        raise ReservationStateError(ERROR_RESERVATION["terminal_revision"])

    data = obj_in.model_dump(exclude_unset=True, exclude={"operator"})
    if not data:
        return get_reservation(db, reservation_id)

    # 合并后的期间必须合法（草稿与已确认路径共用）
    start_year = data.get("start_year", reservation.start_year)
    start_month = data.get("start_month", reservation.start_month)
    end_year = data.get("end_year", reservation.end_year)
    end_month = data.get("end_month", reservation.end_month)
    if (start_year, start_month) > (end_year, end_month):
        raise ReservationStateError("有效期间的起始月份不能晚于结束月份")

    if reservation.status == ReservationStatus.DRAFT:
        before = _snapshot(reservation)
        _apply_revision_fields(reservation, data)
        _add_event(
            db,
            reservation.id,
            ReservationEventType.REVISED,
            actor=obj_in.operator,
            payload={"before": before, "after": _snapshot(reservation)},
        )
        db.commit()
        return get_reservation(db, reservation_id)

    # 已确认：同事务内锁池、校验、只重写未来月份明细
    pool = _lock_pool(db, reservation.park_id, reservation.resource_type)
    reservation = db.get(
        models.CapacityReservation, reservation_id, populate_existing=True
    )
    if reservation.status != ReservationStatus.CONFIRMED:
        raise ReservationStateError(
            fmt(
                ERROR_RESERVATION["invalid_status"],
                status=reservation.status.value,
            )
        )

    before = _snapshot(reservation)
    new_amount = data.get("amount", reservation.amount)
    months = _future_months(start_year, start_month, end_year, end_month, today)
    if not months:
        raise ReservationStateError(ERROR_RESERVATION["period_fully_past"])

    _validate_capacity(
        db, pool, months, new_amount, exclude_reservation_id=reservation.id
    )
    _delete_entries_from(db, reservation.id, month_index(*current_month(today)))
    _insert_entries(db, reservation, months, new_amount)
    _apply_revision_fields(reservation, data)
    _add_event(
        db,
        reservation.id,
        ReservationEventType.REVISED,
        actor=obj_in.operator,
        payload={
            "before": before,
            "after": _snapshot(reservation),
            "note": "历史月份明细保持不变，仅重写当月及以后",
        },
    )
    db.commit()
    return get_reservation(db, reservation_id)


def _apply_revision_fields(
    reservation: models.CapacityReservation, data: dict
) -> None:
    for field in (
        "amount",
        "start_year",
        "start_month",
        "end_year",
        "end_month",
        "stage",
        "remark",
    ):
        if field in data and data[field] is not None:
            setattr(reservation, field, data[field])


# ---------- 释放 ----------


def release_reservation(
    db: Session,
    reservation_id: int,
    amount: Optional[float] = None,
    effective_from: Optional[Tuple[int, int]] = None,
    operator: Optional[str] = None,
    reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    today: Optional[date] = None,
) -> Optional[models.CapacityReservation]:
    replay = _find_event_by_key(db, idempotency_key)
    if (
        replay
        and replay.reservation_id == reservation_id
        and replay.event_type
        in (ReservationEventType.PARTIALLY_RELEASED, ReservationEventType.RELEASED)
    ):
        return get_reservation(db, reservation_id)

    reservation = db.get(models.CapacityReservation, reservation_id)
    if not reservation:
        return None
    _lock_pool(db, reservation.park_id, reservation.resource_type, required=False)
    reservation = db.get(
        models.CapacityReservation, reservation_id, populate_existing=True
    )
    if reservation.status in (ReservationStatus.RELEASED, ReservationStatus.TRANSFERRED):
        return get_reservation(db, reservation_id)  # 重复释放，幂等返回

    cur = month_index(*current_month(today))
    eff = month_index(*effective_from) if effective_from else cur
    if eff < cur:
        raise ReservationStateError(ERROR_RESERVATION["release_past_period"])

    if reservation.status == ReservationStatus.DRAFT:
        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = datetime.utcnow()
        _add_event(
            db,
            reservation.id,
            ReservationEventType.RELEASED,
            actor=operator,
            reason=reason,
            payload={"from_status": ReservationStatus.DRAFT.value},
            idempotency_key=idempotency_key,
        )
        db.commit()
        return get_reservation(db, reservation_id)

    full = amount is None or amount >= reservation.amount - _EPS
    if full:
        released = reservation.amount
        _delete_entries_from(db, reservation.id, eff)
        reservation.released_amount += released
        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = datetime.utcnow()
        _add_event(
            db,
            reservation.id,
            ReservationEventType.RELEASED,
            actor=operator,
            reason=reason,
            payload={"released": released, "effective_from": eff},
            idempotency_key=idempotency_key,
        )
    else:
        new_amount = round(reservation.amount - amount, 6)
        _rewrite_entries_from(db, reservation.id, eff, new_amount)
        reservation.amount = new_amount
        reservation.released_amount += amount
        _add_event(
            db,
            reservation.id,
            ReservationEventType.PARTIALLY_RELEASED,
            actor=operator,
            reason=reason,
            payload={
                "released": amount,
                "remaining": new_amount,
                "effective_from": eff,
            },
            idempotency_key=idempotency_key,
        )
    db.commit()
    return get_reservation(db, reservation_id)


# ---------- 转移 ----------


def transfer_reservation(
    db: Session,
    reservation_id: int,
    target_project_id: int,
    amount: Optional[float] = None,
    operator: Optional[str] = None,
    reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    today: Optional[date] = None,
) -> Optional[Tuple[models.CapacityReservation, models.CapacityReservation]]:
    replay = _find_event_by_key(db, idempotency_key)
    if (
        replay
        and replay.reservation_id == reservation_id
        and replay.event_type == ReservationEventType.TRANSFERRED_OUT
    ):
        payload = json.loads(replay.payload or "{}")
        target_id = payload.get("target_reservation_id")
        source = get_reservation(db, reservation_id)
        target = get_reservation(db, target_id) if target_id else None
        if source and target:
            return source, target

    reservation = db.get(models.CapacityReservation, reservation_id)
    if not reservation:
        return None
    if reservation.status != ReservationStatus.CONFIRMED:
        raise ReservationStateError(
            fmt(
                ERROR_RESERVATION["invalid_status"],
                status=reservation.status.value,
            )
        )
    if target_project_id == reservation.project_id:
        raise ReservationStateError(ERROR_RESERVATION["transfer_self"])
    target_project = db.get(models.Project, target_project_id)
    if not target_project:
        raise ReservationStateError(ERROR_NOT_FOUND["project"])
    if target_project.park_id != reservation.park_id:
        raise ReservationStateError(ERROR_RESERVATION["transfer_park_mismatch"])

    pool = _lock_pool(db, reservation.park_id, reservation.resource_type)
    reservation = db.get(
        models.CapacityReservation, reservation_id, populate_existing=True
    )
    if reservation.status != ReservationStatus.CONFIRMED:
        raise ReservationStateError(
            fmt(
                ERROR_RESERVATION["invalid_status"],
                status=reservation.status.value,
            )
        )

    transfer_amount = amount if amount is not None else reservation.amount
    if transfer_amount > reservation.amount + _EPS:
        raise ReservationStateError(
            fmt(
                ERROR_RESERVATION["transfer_amount_exceeds"],
                amount=reservation.amount,
            )
        )

    cur = month_index(*current_month(today))
    future_month_rows = (
        db.query(
            models.CapacityReservationEntry.year,
            models.CapacityReservationEntry.month,
        )
        .filter(
            models.CapacityReservationEntry.reservation_id == reservation.id,
            _month_idx_expr() >= cur,
        )
        .order_by(
            models.CapacityReservationEntry.year,
            models.CapacityReservationEntry.month,
        )
        .all()
    )
    if not future_month_rows:
        raise ReservationStateError(ERROR_RESERVATION["transfer_no_remaining"])
    months = [(y, m) for y, m in future_month_rows]

    # 同事务内：源预约缩减未来明细 → 校验总量 → 目标预约落账
    _validate_capacity(
        db, pool, months, transfer_amount, exclude_reservation_id=reservation.id
    )

    target_reservation = models.CapacityReservation(
        park_id=reservation.park_id,
        project_id=target_project_id,
        resource_type=reservation.resource_type,
        stage=target_project.status,
        status=ReservationStatus.CONFIRMED,
        amount=transfer_amount,
        unit=reservation.unit,
        start_year=months[0][0],
        start_month=months[0][1],
        end_year=months[-1][0],
        end_month=months[-1][1],
        transfer_from_id=reservation.id,
        operator=operator,
        remark=f"自预约 {reservation.reservation_no} 转入",
        confirmed_at=datetime.utcnow(),
    )
    db.add(target_reservation)
    db.flush()
    target_reservation.reservation_no = (
        f"RSV{datetime.utcnow():%Y%m%d}-{target_reservation.id:05d}"
    )
    _insert_entries(db, target_reservation, months, transfer_amount)

    remaining = round(reservation.amount - transfer_amount, 6)
    if remaining <= _EPS:
        _delete_entries_from(db, reservation.id, cur)
        reservation.status = ReservationStatus.TRANSFERRED
    else:
        _rewrite_entries_from(db, reservation.id, cur, remaining)
        reservation.amount = remaining

    _add_event(
        db,
        reservation.id,
        ReservationEventType.TRANSFERRED_OUT,
        actor=operator,
        reason=reason,
        payload={
            "target_reservation_id": target_reservation.id,
            "target_project_id": target_project_id,
            "amount": transfer_amount,
            "months": len(months),
        },
        idempotency_key=idempotency_key,
    )
    _add_event(
        db,
        target_reservation.id,
        ReservationEventType.TRANSFERRED_IN,
        actor=operator,
        reason=reason,
        payload={
            "source_reservation_id": reservation.id,
            "source_project_id": reservation.project_id,
            "amount": transfer_amount,
            "months": len(months),
        },
        idempotency_key=f"{idempotency_key}:in" if idempotency_key else None,
    )
    db.commit()
    return (
        get_reservation(db, reservation.id),
        get_reservation(db, target_reservation.id),
    )


# ---------- 项目撤回 / 阶段变更的自动释放 ----------


def apply_stage_change_release_rules(
    db: Session,
    project: models.Project,
    from_status: ProjectStatus,
    to_status: ProjectStatus,
    operator: Optional[str] = None,
    today: Optional[date] = None,
) -> List[models.CapacityReservation]:
    """项目撤回或阶段回退时释放未使用额度。

    规则：
    - 阶段前进不释放；
    - 回退到「招商中」视为项目撤回，释放该项目全部活动预约的未使用额度；
    - 其他回退只释放「登记阶段晚于新阶段」的预约（阶段仍然有效的保留）。

    在调用方的事务内执行（项目状态更新已持有写锁），由调用方统一提交。
    """
    order = PROJECT_STATUS_ORDER
    if order.index(to_status) >= order.index(from_status):
        return []

    withdrawn = to_status == ProjectStatus.ATTRACTING_INVESTMENT
    reservations = (
        db.query(models.CapacityReservation)
        .filter(
            models.CapacityReservation.project_id == project.id,
            models.CapacityReservation.status.in_(
                [ReservationStatus.DRAFT, ReservationStatus.CONFIRMED]
            ),
        )
        .all()
    )
    released: List[models.CapacityReservation] = []
    for reservation in reservations:
        if not withdrawn and order.index(reservation.stage) <= order.index(to_status):
            continue
        if reservation.status == ReservationStatus.CONFIRMED:
            _delete_entries_from(db, reservation.id, month_index(*current_month(today)))
            reservation.released_amount += reservation.amount
        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = datetime.utcnow()
        if withdrawn:
            reason = "项目撤回至「招商中」，按规则释放全部未使用额度"
        else:
            reason = (
                f"项目阶段由「{from_status.value}」回退至「{to_status.value}」，"
                f"按规则释放「{reservation.stage.value}」阶段登记的未使用额度"
            )
        _add_event(
            db,
            reservation.id,
            ReservationEventType.AUTO_RELEASED,
            actor=operator,
            reason=reason,
            payload={
                "from_status": from_status.value,
                "to_status": to_status.value,
                "released": reservation.amount,
            },
        )
        released.append(reservation)
    return released
