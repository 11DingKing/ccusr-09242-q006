from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List

from ..database import get_db
from .. import crud, schemas
from ..enums import ResourceType, ReservationStatus
from ..errors import HTTPStatus, ERROR_NOT_FOUND, ERROR_RESERVATION
from ..services import capacity_ledger
from ..services.capacity_ledger import CapacityConflictError, ReservationStateError

router = APIRouter(prefix="/capacity-ledger", tags=["园区资源容量预约台账"])


def _conflict_detail(exc: CapacityConflictError) -> dict:
    return {
        "message": ERROR_RESERVATION["insufficient"],
        "rejections": exc.rejections,
    }


@router.post(
    "/pools",
    response_model=schemas.CapacityPool,
    summary="登记或更新园区资源容量池（用地/供电/污水处理总量）",
)
def upsert_capacity_pool(
    pool_in: schemas.CapacityPoolCreate,
    db: Session = Depends(get_db),
):
    park = crud.get_park(db, park_id=pool_in.park_id)
    if not park:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["park"],
        )
    return capacity_ledger.upsert_pool(db=db, obj_in=pool_in)


@router.get(
    "/pools",
    response_model=List[schemas.CapacityPool],
    summary="查询园区资源容量池列表",
)
def list_capacity_pools(
    park_id: Optional[int] = Query(None, description="园区ID筛选"),
    db: Session = Depends(get_db),
):
    return capacity_ledger.list_pools(db=db, park_id=park_id)


@router.get(
    "/availability",
    response_model=schemas.PoolAvailability,
    summary="查询某资源在期间内逐月可用量与占用来源（新项目录入前核对）",
)
def get_resource_availability(
    park_id: int = Query(...),
    resource_type: ResourceType = Query(...),
    start_year: int = Query(..., ge=2000, le=2100),
    start_month: int = Query(..., ge=1, le=12),
    end_year: int = Query(..., ge=2000, le=2100),
    end_month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    if (start_year, start_month) > (end_year, end_month):
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="有效期间的起始月份不能晚于结束月份",
        )
    result = capacity_ledger.get_availability(
        db=db,
        park_id=park_id,
        resource_type=resource_type,
        start_year=start_year,
        start_month=start_month,
        end_year=end_year,
        end_month=end_month,
    )
    if result is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["capacity_pool"],
        )
    return result


@router.post(
    "/reservations",
    response_model=schemas.CapacityReservationDetail,
    summary="暂存容量预约（草稿，不占用容量；支持幂等键防重复登记）",
)
def create_reservation(
    res_in: schemas.CapacityReservationCreate,
    db: Session = Depends(get_db),
):
    park = crud.get_park(db, park_id=res_in.park_id)
    if not park:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["park"],
        )
    project = crud.get_project(db, project_id=res_in.project_id)
    if not project:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["project"],
        )
    try:
        return capacity_ledger.create_reservation(db=db, obj_in=res_in)
    except ReservationStateError as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))


@router.get(
    "/reservations",
    response_model=List[schemas.CapacityReservationOut],
    summary="查询容量预约列表",
)
def list_reservations(
    park_id: Optional[int] = Query(None, description="园区ID筛选"),
    project_id: Optional[int] = Query(None, description="项目ID筛选"),
    resource_type: Optional[ResourceType] = Query(None, description="资源类型筛选"),
    status: Optional[ReservationStatus] = Query(None, description="预约状态筛选"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return capacity_ledger.list_reservations(
        db=db,
        park_id=park_id,
        project_id=project_id,
        resource_type=resource_type,
        status=status,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/reservations/{reservation_id}",
    response_model=schemas.CapacityReservationDetail,
    summary="查询预约详情（含月度台账明细与操作事件）",
)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    reservation = capacity_ledger.get_reservation(db, reservation_id=reservation_id)
    if not reservation:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    return reservation


@router.put(
    "/reservations/{reservation_id}",
    response_model=schemas.CapacityReservationDetail,
    summary="修订预约（已确认的预约仅重写未来月份台账，历史月份保持不变）",
)
def revise_reservation(
    reservation_id: int,
    res_in: schemas.CapacityReservationUpdate,
    db: Session = Depends(get_db),
):
    try:
        reservation = capacity_ledger.revise_reservation(
            db=db, reservation_id=reservation_id, obj_in=res_in
        )
    except CapacityConflictError as e:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail=_conflict_detail(e)
        )
    except ReservationStateError as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    if not reservation:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    return reservation


@router.post(
    "/reservations/{reservation_id}/confirm",
    response_model=schemas.CapacityReservationDetail,
    summary="确认预约（一笔事务内校验总量与既有承诺，不足时返回每个拒绝项的来源与剩余量）",
)
def confirm_reservation(
    reservation_id: int,
    action: schemas.ReservationActionRequest = None,
    db: Session = Depends(get_db),
):
    action = action or schemas.ReservationActionRequest()
    try:
        reservation = capacity_ledger.confirm_reservation(
            db=db,
            reservation_id=reservation_id,
            operator=action.operator,
            idempotency_key=action.idempotency_key,
        )
    except CapacityConflictError as e:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail=_conflict_detail(e)
        )
    except ReservationStateError as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    if not reservation:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    return reservation


@router.post(
    "/reservations/{reservation_id}/release",
    response_model=schemas.CapacityReservationDetail,
    summary="释放预约（支持部分释放与生效月份；历史月份不可释放）",
)
def release_reservation(
    reservation_id: int,
    action: schemas.ReservationReleaseRequest = None,
    db: Session = Depends(get_db),
):
    action = action or schemas.ReservationReleaseRequest()
    effective_from = None
    if action.effective_from_year is not None or action.effective_from_month is not None:
        if action.effective_from_year is None or action.effective_from_month is None:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="释放生效年份与月份需同时提供",
            )
        effective_from = (action.effective_from_year, action.effective_from_month)
    try:
        reservation = capacity_ledger.release_reservation(
            db=db,
            reservation_id=reservation_id,
            amount=action.amount,
            effective_from=effective_from,
            operator=action.operator,
            reason=action.reason,
            idempotency_key=action.idempotency_key,
        )
    except ReservationStateError as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    if not reservation:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    return reservation


@router.post(
    "/reservations/{reservation_id}/transfer",
    response_model=schemas.ReservationTransferResult,
    summary="将预约额度转移给同园区的其他项目（可部分转移）",
)
def transfer_reservation(
    reservation_id: int,
    action: schemas.ReservationTransferRequest,
    db: Session = Depends(get_db),
):
    target = crud.get_project(db, project_id=action.target_project_id)
    if not target:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["project"],
        )
    try:
        result = capacity_ledger.transfer_reservation(
            db=db,
            reservation_id=reservation_id,
            target_project_id=action.target_project_id,
            amount=action.amount,
            operator=action.operator,
            reason=action.reason,
            idempotency_key=action.idempotency_key,
        )
    except CapacityConflictError as e:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT, detail=_conflict_detail(e)
        )
    except ReservationStateError as e:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(e))
    if not result:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["reservation"],
        )
    source, target_reservation = result
    return {"source": source, "target": target_reservation}
